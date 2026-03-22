"""DevOps node — Generates CI/CD pipelines and deployment artifacts.

Uses the same chunked per-file approach as the developer node:

  Step 1  —  Decide: does the project need CI and/or CD?
  Step 2  —  Plan:   ask the LLM which files to generate from the known set.
  Step 3  —  Generate: call the LLM once per file with targeted hints.
"""

from __future__ import annotations

from cancel_token import raise_if_cancelled
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from graph.prompts.devops_prompts import (
    CD_SYSTEM_PROMPT,
    CI_SYSTEM_PROMPT,
    DECISION_PROMPT,
    FILE_HINT,
)
from graph.state import AgentState, SingleFileContent
from integrations.cortex import get_cortex_llm


# ── Known file sets (path → FILE_HINT key) ────────────────────────────────────

_CI_FILE_HINTS: dict[str, str] = {
    ".github/workflows/ci.yml": FILE_HINT["ci_workflow"],
    "pyproject.toml": FILE_HINT["pyproject_toml"],
    ".gitignore": FILE_HINT["gitignore"],
    "Makefile": FILE_HINT["makefile"],
}

_CD_FILE_HINTS: dict[str, str] = {
    ".github/workflows/cd.yml": FILE_HINT["cd_workflow"],
    "Dockerfile": FILE_HINT["dockerfile"],
    "docker-compose.yml": FILE_HINT["docker_compose"],
    ".env": FILE_HINT["env"],
    ".env.example": FILE_HINT["env_example"],
    ".dockerignore": FILE_HINT["dockerignore"],
}


# ── Structured output models ──────────────────────────────────────────────────


class DevOpsDecision(BaseModel):
    """Structured output for the CI/CD scope decision."""

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
    reasoning: str = Field(
        description="One-sentence justification for this decision."
    )


class DevOpsFilePlan(BaseModel):
    """File selection made by the LLM for a given project."""

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
    """Remove markdown code fences and leading preamble from LLM output.

    Strips bold title lines and blank lines before the opening fence, then
    removes the opening fence line (e.g. ```yaml) and closing fence.

    Args:
        content: Raw string returned by the LLM.

    Returns:
        Clean file content with no surrounding markdown.
    """
    content = content.strip()
    lines = content.splitlines()
    while lines and (
        lines[0].strip() == "" or lines[0].strip().startswith("**")
    ):
        lines.pop(0)
    content = "\n".join(lines)
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1 :]
    if content.rstrip().endswith("```"):
        content = content.rstrip()[:-3].rstrip()
    return content


def _decide(llm: BaseChatModel, context: str) -> DevOpsDecision:
    """Ask the LLM whether CI and/or CD artifacts are needed for this project.

    Uses structured output to classify the project. Falls back to full CI+CD
    generation if structured output fails.

    Args:
        llm: The language model to use.
        context: Formatted specs + source file list string.

    Returns:
        A DevOpsDecision with needs_ci, needs_cd, and a one-sentence reasoning.
    """
    try:
        return llm.with_structured_output(DevOpsDecision).invoke(
            [
                SystemMessage(content=DECISION_PROMPT),
                HumanMessage(content=context),
            ]
        )
    except Exception:
        return DevOpsDecision(
            needs_ci=True,
            needs_cd=True,
            reasoning="Fallback: structured output unavailable — generating full CI+CD.",
        )


def _plan_files(
    llm: BaseChatModel, context: str, needs_cd: bool
) -> DevOpsFilePlan:
    """Ask the LLM to select which files to generate from the known sets.

    The LLM chooses a relevant subset of _CI_FILE_HINTS and _CD_FILE_HINTS
    based on the project specs. Falls back to the full known sets if structured
    output fails.

    Args:
        llm: The language model to use.
        context: Formatted specs + source file list string.
        needs_cd: Whether CD files should be considered.

    Returns:
        A DevOpsFilePlan with the selected ci_files and cd_files paths.
    """
    plan_prompt = (
        f"{context}\n\n"
        "Select which files to generate for this project.\n"
        f"Available CI files: {list(_CI_FILE_HINTS)}\n"
        f"Available CD files: {list(_CD_FILE_HINTS) if needs_cd else '(not needed)'}\n"
        "Only include files that are relevant to the project."
    )
    try:
        return llm.with_structured_output(DevOpsFilePlan).invoke(
            [
                HumanMessage(content=plan_prompt),
            ]
        )
    except Exception:
        return DevOpsFilePlan(
            ci_files=list(_CI_FILE_HINTS),
            cd_files=list(_CD_FILE_HINTS) if needs_cd else [],
        )


def _generate_file(
    llm: BaseChatModel,
    system_prompt: str,
    context: str,
    file_path: str,
    hint: str,
) -> str:
    """Generate the content of a single DevOps file using the LLM.

    Tries structured output (SingleFileContent) first, then falls back to a
    raw completion if structured output fails.

    Args:
        llm: The language model to use.
        system_prompt: CI or CD system prompt to set the agent role.
        context: Formatted specs + source file list string.
        file_path: Relative path of the file being generated.
        hint: File-specific instructions from FILE_HINT.

    Returns:
        The generated file content, or an empty string if both attempts fail.
    """
    user_msg = (
        f"{context}\n\n"
        f"## File to generate\nPath: `{file_path}`\n\n"
        f"## Instructions\n{hint}"
    )
    try:
        result: SingleFileContent = llm.with_structured_output(
            SingleFileContent
        ).invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_msg),
            ]
        )
        return _strip_fences(result.content)
    except Exception:
        try:
            response = llm.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_msg),
                ]
            )
            return _strip_fences(response.content)
        except Exception:
            return ""


# ── Main node ─────────────────────────────────────────────────────────────────


def devops_node(state: AgentState) -> dict:
    """LangGraph node: run the DevOps agent with chunked per-file generation.

    Args:
        state: Current pipeline state with ``specs`` and ``code_files``.

    Returns:
        A dict updating ``ci_files``, ``cd_files``, ``needs_ci``, ``needs_cd``,
        and ``reasoning_logs``.
    """
    raise_if_cancelled()
    print("\n" + "=" * 60)
    print("⚙️  DEVOPS AGENT — Generating CI/CD & Docker")
    print("=" * 60)

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.1, max_tokens=4096)

    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    file_list = (
        "\n".join(
            f"- {f['path'] if isinstance(f, dict) else f.path} "
            f"({f['language'] if isinstance(f, dict) else f.language})"
            for f in code_files
        )
        or "(none yet)"
    )
    context = (
        f"## Application Specifications\n{specs}\n\n"
        f"## Generated Source Files\n{file_list}"
    )

    # ── Plan phase: decide CI/CD scope ────────────────────────────────────────
    print("\n[PLAN] Deciding CI/CD scope …")
    decision = _decide(llm, context)
    plan_trace = (
        f"CI={decision.needs_ci}, CD={decision.needs_cd}. {decision.reasoning}"
    )
    print(f"[PLAN] {plan_trace}")

    if not decision.needs_ci:
        print("[SKIP] No CI/CD artifacts needed for this project type.\n")
        updates: dict = {
            "ci_files": [],
            "cd_files": [],
            "needs_ci": False,
            "needs_cd": False,
            "reasoning_logs": [
                {"agent": "devops", "phase": "plan", "content": plan_trace},
                {
                    "agent": "devops",
                    "phase": "act",
                    "content": "Skipped — no CI/CD required.",
                },
                {
                    "agent": "devops",
                    "phase": "reason",
                    "content": "Project is a pure library or script with no service.",
                },
            ],
        }
        return updates

    # ── Plan phase: select files ───────────────────────────────────────────────
    print("[PLAN] Selecting files to generate …")
    file_plan = _plan_files(llm, context, decision.needs_cd)

    # Ensure .github/workflows/ci.yml is always present when CI is needed
    if ".github/workflows/ci.yml" not in file_plan.ci_files:
        file_plan.ci_files.insert(0, ".github/workflows/ci.yml")

    print(f"[PLAN] CI files  ({len(file_plan.ci_files)}): {file_plan.ci_files}")
    print(f"[PLAN] CD files  ({len(file_plan.cd_files)}): {file_plan.cd_files}")

    # ── Act phase: generate each CI file ──────────────────────────────────────
    ci_files: list[dict] = []
    for i, path in enumerate(file_plan.ci_files, 1):
        hint = _CI_FILE_HINTS.get(path)
        if hint is None:
            print(f"[ACT]  Unknown CI file '{path}', skipping.")
            continue
        print(f"[ACT]  CI {i}/{len(file_plan.ci_files)}: {path} …")
        content = _generate_file(llm, CI_SYSTEM_PROMPT, context, path, hint)
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
                print(
                    f"[ACT]  Unknown CD file '{path}', letting agent decide content."
                )
                hint = f"Generate the complete, production-ready content for: {path}"
            print(f"[ACT]  CD {i}/{len(file_plan.cd_files)}: {path} …")
            content = _generate_file(llm, CD_SYSTEM_PROMPT, context, path, hint)
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
            {"agent": "devops", "phase": "plan", "content": plan_trace},
            {
                "agent": "devops",
                "phase": "act",
                "content": f"Generated {len(ci_files)} CI + {len(cd_files)} CD files.",
            },
            {"agent": "devops", "phase": "reason", "content": reason_trace},
        ],
    }
