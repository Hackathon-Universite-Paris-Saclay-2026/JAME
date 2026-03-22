"""DevOps Agent — Generates CI/CD pipelines and deployment artifacts.

Uses a chunked per-file approach:

  Step 1  —  Decide: does the project need CI and/or CD?
  Step 2  —  Plan:   ask the LLM which files to generate from the known set.
  Step 3  —  Generate: call the LLM once per file with targeted hints.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from state import AgentState, SingleFileContent


# ── Prompts (loaded from prompts/devops.yaml) ────────────────────────────────

_PROMPTS_FILE = Path(__file__).parent.parent / "prompts" / "devops.yaml"


def _load_prompts() -> dict:
    with open(_PROMPTS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


_p    = _load_prompts()
_hint = _p["file_hint"]

_DECISION_PROMPT  = _p["decision"]["classify"]
_CI_SYSTEM_PROMPT = _p["generate"]["ci_system"]
_CD_SYSTEM_PROMPT = _p["generate"]["cd_system"]

# Inject pinned GitHub Actions SHAs into the ci_workflow hint
_sha = {
    "checkout":     _p["actions"]["checkout"],
    "setup_python": _p["actions"]["setup_python"],
    "cache":        _p["actions"]["cache"],
}
_hint["ci_workflow"] = _hint["ci_workflow"].format(**_sha)

FILE_HINT: dict[str, str] = _hint


# ── Known file sets with their generation hints ───────────────────────────────

_CI_FILE_HINTS: dict[str, str] = {
    ".github/workflows/ci.yml": FILE_HINT["ci_workflow"],
    "pyproject.toml":           FILE_HINT["pyproject_toml"],
    ".gitignore":               FILE_HINT["gitignore"],
    "Makefile":                 FILE_HINT["makefile"],
}

_CD_FILE_HINTS: dict[str, str] = {
    ".github/workflows/cd.yml": FILE_HINT["cd_workflow"],
    "Dockerfile":               FILE_HINT["dockerfile"],
    "docker-compose.yml":       FILE_HINT["docker_compose"],
    ".env":                     FILE_HINT["env"],
    ".env.example":             FILE_HINT["env_example"],
    ".dockerignore":            FILE_HINT["dockerignore"],
}


# ── Structured output models ──────────────────────────────────────────────────


class DevOpsDecision(BaseModel):
    needs_ci: bool = Field(
        description=(
            "True only if the project has multiple source files that import from each other. "
            "False for a single function, utility, or script — even when a test file exists."
        )
    )
    needs_cd: bool = Field(
        description=(
            "True if the project needs containerized deployment artifacts. "
            "True only when the project exposes a network service: API, web app, worker, daemon. "
            "False for pure libraries, utility functions, or scripts with no server."
        )
    )
    reasoning: str = Field(description="One-sentence justification for this decision.")


class DevOpsFilePlan(BaseModel):
    ci_files: list[str] = Field(
        default_factory=list,
        description=(
            "CI file paths to generate. Choose from: "
            + ", ".join(f"`{p}`" for p in _CI_FILE_HINTS)
        ),
    )
    cd_files: list[str] = Field(
        default_factory=list,
        description=(
            "CD file paths to generate. Choose from: "
            + ", ".join(f"`{p}`" for p in _CD_FILE_HINTS)
            + ". Leave empty when needs_cd is false."
        ),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _strip_fences(content: str) -> str:
    content = content.strip()
    # Drop any leading markdown preamble lines (bold titles, blank lines) before the fence
    lines = content.splitlines()
    while lines and (lines[0].strip() == "" or lines[0].strip().startswith("**")):
        lines.pop(0)
    content = "\n".join(lines)
    # Strip opening fence line (e.g. ```yaml or ```)
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1:]
    # Strip closing fence
    if content.rstrip().endswith("```"):
        content = content.rstrip()[:-3].rstrip()
    return content


def _get_llm() -> ChatOpenAI:
    api_key = os.getenv("SNOWFLAKE_API_KEY", "")
    api_base = os.getenv("SNOWFLAKE_API_BASE", "")
    
    # Ensure ASCII-safe encoding for HTTP headers
    try:
        api_key = api_key.encode('ascii').decode('ascii')
        api_base = api_base.encode('ascii').decode('ascii')
    except UnicodeEncodeError as e:
        print(f"[WARNING] Non-ASCII characters in API credentials at position {e.start}: '{e.object[e.start:e.end]}'")
        # Remove non-ASCII characters as fallback
        api_key = api_key.encode('ascii', errors='ignore').decode('ascii')
        api_base = api_base.encode('ascii', errors='ignore').decode('ascii')
    
    return ChatOpenAI(
        model="llama3.1-70b",
        temperature=0.1,
        max_tokens=4096,
        openai_api_key=api_key,
        openai_api_base=api_base,
    )


def _decide(llm: ChatOpenAI, context: str) -> DevOpsDecision:
    try:
        return llm.with_structured_output(DevOpsDecision).invoke([
            SystemMessage(content=_DECISION_PROMPT),
            HumanMessage(content=context),
        ])
    except Exception as e:
        print(f"[ERROR] _decide failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return DevOpsDecision(
            needs_ci=True,
            needs_cd=True,
            reasoning="Fallback: structured output unavailable — generating full CI+CD.",
        )


def _plan_files(llm: ChatOpenAI, context: str, needs_cd: bool) -> DevOpsFilePlan:
    plan_prompt = (
        f"{context}\n\n"
        "Select which files to generate for this project.\n"
        f"Available CI files: {list(_CI_FILE_HINTS)}\n"
        f"Available CD files: {list(_CD_FILE_HINTS) if needs_cd else '(not needed)'}\n"
        "Only include files that are relevant to the project."
    )
    try:
        return llm.with_structured_output(DevOpsFilePlan).invoke([
            HumanMessage(content=plan_prompt),
        ])
    except Exception:
        return DevOpsFilePlan(
            ci_files=list(_CI_FILE_HINTS),
            cd_files=list(_CD_FILE_HINTS) if needs_cd else [],
        )


def _generate_file(
    llm: ChatOpenAI,
    system_prompt: str,
    context: str,
    file_path: str,
    hint: str,
) -> str:
    user_msg = (
        f"{context}\n\n"
        f"## File to generate\nPath: `{file_path}`\n\n"
        f"## Instructions\n{hint}"
    )
    try:
        result: SingleFileContent = llm.with_structured_output(SingleFileContent).invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_msg),
        ])
        return _strip_fences(result.content)
    except Exception:
        try:
            response = llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_msg),
            ])
            return _strip_fences(response.content)
        except Exception:
            return ""


# ── Main node ─────────────────────────────────────────────────────────────────


def devops_node(state: AgentState) -> dict:
    """LangGraph node: run the DevOps agent with chunked per-file generation."""

    print("\n" + "=" * 60)
    print("⚙️  DEVOPS AGENT — Generating CI/CD & Docker")
    print("=" * 60)

    llm = _get_llm()

    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    file_list = (
        "\n".join(f"- {f['path']} ({f['language']})" for f in code_files)
        or "(none yet)"
    )
    context = (
        f"## Application Specifications\n{specs}\n\n"
        f"## Generated Source Files\n{file_list}"
    )

    # ── Plan phase: decide CI/CD scope ────────────────────────────────────────
    print("\n[PLAN] Deciding CI/CD scope …")
    decision = _decide(llm, context)
    plan_trace = f"CI={decision.needs_ci}, CD={decision.needs_cd}. {decision.reasoning}"
    print(f"[PLAN] {plan_trace}")

    if not decision.needs_ci:
        print("[SKIP] No CI/CD artifacts needed for this project type.\n")
        return {
            "ci_files": [],
            "cd_files": [],
            "needs_ci": False,
            "needs_cd": False,
            "reasoning_logs": [
                {"agent": "devops", "phase": "plan",   "content": plan_trace},
                {"agent": "devops", "phase": "act",    "content": "Skipped — no CI/CD required."},
                {"agent": "devops", "phase": "reason", "content": "Project is a pure library or script with no service."},
            ],
        }

    # ── Plan phase: select files ───────────────────────────────────────────────
    print("[PLAN] Selecting files to generate …")
    file_plan = _plan_files(llm, context, decision.needs_cd)

    if ".github/workflows/ci.yml" not in file_plan.ci_files:
        file_plan.ci_files.insert(0, ".github/workflows/ci.yml")

    print(f"[PLAN] CI files ({len(file_plan.ci_files)}): {file_plan.ci_files}")
    print(f"[PLAN] CD files ({len(file_plan.cd_files)}): {file_plan.cd_files}")

    # ── Act phase: generate each CI file ──────────────────────────────────────
    ci_files: list[dict] = []
    for i, path in enumerate(file_plan.ci_files, 1):
        hint = _CI_FILE_HINTS.get(path)
        if hint is None:
            print(f"[ACT]  Unknown CI file '{path}', skipping.")
            continue
        print(f"[ACT]  CI {i}/{len(file_plan.ci_files)}: {path} …")
        content = _generate_file(llm, _CI_SYSTEM_PROMPT, context, path, hint)
        if content.strip():
            ci_files.append({"path": path, "content": content})
            print(f"         ✓ {len(content)} chars")
        else:
            print("         ✗ Empty content, skipping")

    # ── Act phase: generate each CD file ──────────────────────────────────────
    cd_files: list[dict] = []
    if decision.needs_cd:
        for i, path in enumerate(file_plan.cd_files, 1):
            hint = _CD_FILE_HINTS.get(path)
            if hint is None:
                print(f"[ACT]  Unknown CD file '{path}', let agent decide content for now.")
                hint = "(No hint available)"
            print(f"[ACT]  CD {i}/{len(file_plan.cd_files)}: {path} …")
            content = _generate_file(llm, _CD_SYSTEM_PROMPT, context, path, hint)
            if content.strip():
                cd_files.append({"path": path, "content": content})
                print(f"         ✓ {len(content)} chars")
            else:
                print("         ✗ Empty content, skipping")

    # ── Reason phase ──────────────────────────────────────────────────────────
    ci_summary = ", ".join(f["path"] for f in ci_files) or "(none)"
    cd_summary = ", ".join(f["path"] for f in cd_files) or "(none)"
    reason_trace = f"CI: {ci_summary} | CD: {cd_summary}"
    print(f"\n[REASON] {reason_trace}\n")

    return {
        "ci_files": ci_files,
        "cd_files": cd_files,
        "needs_ci": decision.needs_ci,
        "needs_cd": decision.needs_cd,
        "reasoning_logs": [
            {"agent": "devops", "phase": "plan",   "content": plan_trace},
            {"agent": "devops", "phase": "act",    "content": f"Generated {len(ci_files)} CI + {len(cd_files)} CD files."},
            {"agent": "devops", "phase": "reason", "content": reason_trace},
        ],
    }
