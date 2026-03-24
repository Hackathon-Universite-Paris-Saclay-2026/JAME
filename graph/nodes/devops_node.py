"""DevOps node — Generates CI/CD pipelines and deployment artifacts.

Uses the same chunked per-file approach as the developer node:

  Step 1  —  Decide: does the project need CI and/or CD?
  Step 2  —  Plan:   ask the LLM which files to generate from the known set.
  Step 3  —  Generate: call the LLM once per file with targeted hints.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from cancel_token import raise_if_cancelled
from graph.prompts.devops_prompts import (
    CD_SYSTEM_PROMPT,
    CI_SYSTEM_PROMPT,
    DECISION_PROMPT,
    FILE_HINT,
    MEMORY_COMPRESS_PROMPT,
)
from graph.state import AgentState, SingleFileContent
from integrations.cortex import get_cortex_llm
from utils.node import maybe_compress, run_parallel


# ── Known file sets (path → FILE_HINT key) ────────────────────────────────────

_CI_FILE_HINTS: dict[str, str] = {
    ".github/workflows/ci.yml": FILE_HINT["ci_workflow"],
    "requirements-dev.txt": FILE_HINT["requirements_dev"],
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

# ── Per-file token budgets ─────────────────────────────────────────────────────
# Small config files waste budget at 4096; workflow YAMLs genuinely need it.

_FILE_MAX_TOKENS: dict[str, int] = {
    # Tiny — list of patterns, no logic
    ".gitignore": 512,
    ".dockerignore": 512,
    ".env": 512,
    ".env.example": 512,
    "requirements-dev.txt": 256,
    # Medium — structured config, some verbosity
    "Makefile": 1024,
    "pyproject.toml": 1024,
    "Dockerfile": 2048,
    "docker-compose.yml": 2048,
    # Large — full CI/CD pipeline YAML with multiple jobs and steps
    ".github/workflows/ci.yml": 4096,
    ".github/workflows/cd.yml": 4096,
}


def _get_max_tokens(file_path: str) -> int:
    """Return the token budget for a given DevOps file, defaulting to 2048."""
    return _FILE_MAX_TOKENS.get(file_path, 2048)


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


def _collect_file_tasks(
    llm: BaseChatModel,
    context: str,
    paths: list[str],
    hint_map: dict[str, str],
    system_prompt: str,
    label: str,
    project_dir: Path | None = None,
) -> tuple[list, list[tuple[str, str]]]:
    """Build parallel task list for a set of DevOps files.

    Returns a ``(tasks, meta)`` pair where *tasks* are zero-argument callables
    ready for ``run_parallel`` and *meta* is a matching list of ``(label, path)``
    tuples for result collection.
    """
    tasks = []
    meta: list[tuple[str, str]] = []
    for path in paths:
        hint = hint_map.get(path) or (
            f"Generate the complete, production-ready content for: {path}"
        )
        # Bind a file-specific token budget — avoids wasting the full 4096
        # budget on tiny files like .gitignore or .env.
        bound_llm = llm.bind(max_tokens=_get_max_tokens(path))
        tasks.append(
            partial(
                _generate_file,
                bound_llm,
                system_prompt,
                context,
                path,
                hint,
                project_dir,
            )
        )
        meta.append((label, path))
    return tasks, meta


def _is_degenerate(content: str, file_path: str) -> bool:
    """Return True when the LLM returned a placeholder instead of real content.

    Catches the case where deepseek-r1 echoes the filename (or its basename)
    into the content field instead of generating actual file content.
    """
    stripped = content.strip()
    basename = file_path.split("/")[-1]
    return stripped in (file_path, basename) or len(stripped) < 10


def _generate_file(
    llm: BaseChatModel,
    system_prompt: str,
    context: str,
    file_path: str,
    hint: str,
    project_dir: Path | None = None,
) -> str:
    """Generate the content of a single DevOps file using the LLM.

    Tries structured output (SingleFileContent) first, falls back to a raw
    completion on failure or degenerate output, then retries with an explicit
    prompt if the result is still degenerate.

    Args:
        llm: The language model to use.
        system_prompt: CI or CD system prompt to set the agent role.
        context: Formatted specs + source file list string.
        file_path: Relative path of the file being generated.
        hint: File-specific instructions from FILE_HINT.
        project_dir: If provided, write the file to disk immediately.

    Returns:
        The generated file content, or an empty string if all attempts fail.
    """
    user_msg = (
        f"{context}\n\n"
        f"## File to generate\nPath: `{file_path}`\n\n"
        f"## Instructions\n{hint}"
    )
    content = ""
    try:
        result: SingleFileContent = llm.with_structured_output(
            SingleFileContent
        ).invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_msg),
            ]
        )
        content = _strip_fences(result.content)
    except Exception as exc:
        print(f"[ACT]  structured output failed for {file_path}: {exc}")

    if not content or _is_degenerate(content, file_path):
        try:
            response = llm.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_msg),
                ]
            )
            content = _strip_fences(response.content)
        except Exception as exc2:
            print(f"[ACT]  raw completion failed for {file_path}: {exc2}")
            return ""

    # Last-chance retry with an explicit prompt if still degenerate
    if _is_degenerate(content, file_path):
        print(
            f"[ACT]  degenerate output for {file_path} — retrying with explicit prompt"
        )
        try:
            retry_msg = (
                f"Generate the complete content of the file `{file_path}`.\n\n"
                f"{hint}\n\n"
                "Return ONLY the raw file content. Do NOT return the filename. "
                "Do NOT use markdown fences."
            )
            response = llm.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=retry_msg),
                ]
            )
            content = _strip_fences(response.content)
        except Exception as exc3:
            print(f"[ACT]  retry failed for {file_path}: {exc3}")
            return ""

    if project_dir is not None and content.strip():
        dest = project_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    return content


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

    _emit = state.get("emit_callback")

    def _log(entry: dict) -> None:
        if _emit:
            _emit(entry)

    _log({"agent": "devops", "phase": "plan", "content": "DevOps agent starting…"})
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
    # Full context for decision and planning (needs complete specs for accuracy).
    context = (
        f"## Application Specifications\n{specs}\n\n"
        f"## Generated Source Files\n{file_list}"
    )
    # Compressed specs for file generation — avoids hitting Cortex input limits
    # on large projects while preserving the DevOps-relevant information.
    compressed_specs = maybe_compress(
        llm, specs, "", MEMORY_COMPRESS_PROMPT, threshold=5000
    )
    generation_context = (
        f"## Application Specifications\n{compressed_specs}\n\n"
        f"## Generated Source Files\n{file_list}"
    )

    # ── Plan phase: decide CI/CD scope ────────────────────────────────────────
    print("\n[PLAN] Deciding CI/CD scope …")
    _log({"agent": "devops", "phase": "plan", "content": "Deciding CI/CD scope…"})
    decision = _decide(llm, context)
    plan_trace = (
        f"CI={decision.needs_ci}, CD={decision.needs_cd}. {decision.reasoning}"
    )
    print(f"[PLAN] {plan_trace}")
    _log({"agent": "devops", "phase": "plan", "content": plan_trace})

    if not decision.needs_ci:
        print("[SKIP] No CI/CD artifacts needed for this project type.\n")
        skip_msg = "Skipped — no CI/CD required."
        reason_msg = "Project is a pure library or script with no service."
        _log({"agent": "devops", "phase": "act", "content": skip_msg})
        _log({"agent": "devops", "phase": "reason", "content": reason_msg})
        updates: dict = {
            "ci_files": [],
            "cd_files": [],
            "needs_ci": False,
            "needs_cd": False,
            "reasoning_logs": [
                {"agent": "devops", "phase": "plan", "content": plan_trace},
                {"agent": "devops", "phase": "act", "content": skip_msg},
                {"agent": "devops", "phase": "reason", "content": reason_msg},
            ],
        }
        return updates

    # ── Plan phase: select files ───────────────────────────────────────────────
    print("[PLAN] Selecting files to generate …")
    _log({"agent": "devops", "phase": "plan", "content": "Selecting files to generate…"})
    file_plan = _plan_files(llm, context, decision.needs_cd)

    # Ensure mandatory workflow files are always present
    if ".github/workflows/ci.yml" not in file_plan.ci_files:
        file_plan.ci_files.insert(0, ".github/workflows/ci.yml")
    if (
        decision.needs_cd
        and ".github/workflows/cd.yml" not in file_plan.cd_files
    ):
        file_plan.cd_files.insert(0, ".github/workflows/cd.yml")

    print(f"[PLAN] CI files  ({len(file_plan.ci_files)}): {file_plan.ci_files}")
    print(f"[PLAN] CD files  ({len(file_plan.cd_files)}): {file_plan.cd_files}")

    # ── Act phase: generate all CI + CD files in parallel ─────────────────────
    # CI and CD files are fully independent — build a flat task list and run
    # everything at once (CI first, then CD for predictable log ordering).
    run_output_dir = state.get("run_output_dir", "")
    project_dir: Path | None = None
    if run_output_dir:
        project_dir = (Path(run_output_dir) / "output" / "project").resolve()
        project_dir.mkdir(parents=True, exist_ok=True)

    ci_tasks, ci_meta = _collect_file_tasks(
        llm,
        generation_context,
        file_plan.ci_files,
        _CI_FILE_HINTS,
        CI_SYSTEM_PROMPT,
        "CI",
        project_dir,
    )
    cd_tasks, cd_meta = (
        _collect_file_tasks(
            llm,
            generation_context,
            file_plan.cd_files,
            _CD_FILE_HINTS,
            CD_SYSTEM_PROMPT,
            "CD",
            project_dir,
        )
        if decision.needs_cd
        else ([], [])
    )

    tasks = ci_tasks + cd_tasks
    task_meta = ci_meta + cd_meta
    print(f"[ACT]  Generating {len(tasks)} file(s) in parallel …")
    _log({"agent": "devops", "phase": "act", "content": f"Generating {len(tasks)} CI/CD file(s) in parallel…"})
    contents: list[str] = run_parallel(tasks)

    ci_files: list[dict] = []
    cd_files: list[dict] = []
    for (label, path), content in zip(task_meta, contents, strict=True):
        if content.strip():
            entry = {"path": path, "content": content}
            (ci_files if label == "CI" else cd_files).append(entry)
            print(f"[ACT]  {label} ✓ {path} ({len(content)} chars)")
        else:
            print(f"[ACT]  {label} ✗ {path} — empty content, skipping")

    # ── Reason phase ──────────────────────────────────────────────────────────
    ci_summary = ", ".join(f["path"] for f in ci_files) or "(none)"
    cd_summary = ", ".join(f["path"] for f in cd_files) or "(none)"
    reason_trace = f"CI: {ci_summary} | CD: {cd_summary}"
    print(f"\n[REASON] {reason_trace}\n")
    _log({"agent": "devops", "phase": "act", "content": f"Generated {len(ci_files)} CI + {len(cd_files)} CD files."})
    _log({"agent": "devops", "phase": "reason", "content": reason_trace})

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
