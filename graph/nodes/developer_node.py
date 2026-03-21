"""Developer node — Generates application code and tests.

Uses a two-step chunked approach to avoid token-limit and JSON-truncation
errors on large generations:

  Step 1  —  Ask the LLM for a *file plan* (list of paths to create).
  Step 2  —  Loop through those paths and call the LLM *once per file*
             to generate its content via structured output.

Architecture enforced across all generated projects:
  Router → Service → Repository/DB → Model
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from graph.prompts.developer_prompts import (
    COMPONENT_HINT,
    FILE_CONTEXT,
    GENERATE_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
    ROUTER_HINT,
    SERVICE_HINT,
)
from graph.state import (
    AgentState,
    FilePlan,
    SingleFileContent,
    _sanitize_path,
)
from integrations.cortex import get_cortex_llm


# ---------------------------------------------------------------------------
# Mandatory files — always present in every generated project
# ---------------------------------------------------------------------------

MANDATORY_FILES = [
    "backend/main.py",
    "backend/database.py",
    "backend/models.py",
    "backend/requirements.txt",
    "tests/conftest.py",
    "tests/test_main.py",
    "frontend/src/App.js",
    "frontend/package.json",
]

# ---------------------------------------------------------------------------
# Import graph — prevents circular dependencies across generated files.
# Each key is a file pattern; value is the list of local modules it may import.
# ---------------------------------------------------------------------------

IMPORT_GRAPH: dict[str, list[str]] = {
    "backend/main.py": ["backend/routers/*", "backend/database.py"],
    "backend/routers/*.py": [
        "backend/services/*.py",
        "backend/models.py",
        "backend/database.py",
    ],
    "backend/services/*.py": ["backend/models.py", "backend/database.py"],
    "backend/models.py": [],
    "backend/database.py": ["backend/models.py"],
    "tests/conftest.py": ["backend/main.py", "backend/database.py"],
    "tests/*.py": [
        "backend/main.py",
        "backend/database.py",
        "tests/conftest.py",
    ],
    "frontend/src/components/*": ["(props only — no cross-component imports)"],
    "frontend/src/App.js": ["frontend/src/components/*"],
}


def _resolve_import_graph(file_path: str) -> list[str]:
    """Return the allowed local imports for a given file path.

    Matches exact keys first, then falls back to wildcard patterns.

    Args:
        file_path: Sanitized relative path of the file being generated.

    Returns:
        List of allowed import targets as human-readable strings.
    """
    if file_path in IMPORT_GRAPH:
        return IMPORT_GRAPH[file_path]
    for pattern, deps in IMPORT_GRAPH.items():
        if "*" in pattern:
            prefix = pattern.split("*")[0]
            if file_path.startswith(prefix):
                return deps
    return []


def _get_file_hint(file_path: str) -> str:
    """Return the generation hint for a given file path.

    Checks the static FILE_CONTEXT dict first, then falls back to
    pattern-based hints for routers, services, and React components.

    Args:
        file_path: Sanitized relative path of the file being generated.

    Returns:
        A string instruction describing what the file should contain.
    """
    if file_path in FILE_CONTEXT:
        return FILE_CONTEXT[file_path]
    if "routers/" in file_path:
        return ROUTER_HINT
    if "services/" in file_path:
        return SERVICE_HINT
    if "components/" in file_path:
        return COMPONENT_HINT
    return f"Generate the complete, production-quality content for: {file_path}"


def _detect_language(path: str) -> str:
    """Infer the programming language from a file's extension.

    Args:
        path: Relative file path.

    Returns:
        A lowercase language string suitable for syntax highlighting.
    """
    ext_map = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".txt": "text",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
    }
    for ext, lang in ext_map.items():
        if path.endswith(ext):
            return lang
    return "text"


def _strip_markdown_fences(content: str) -> str:
    """Remove wrapping ``` fences if the LLM added them despite instructions.

    Args:
        content: Raw string returned by the LLM.

    Returns:
        Content with opening and closing fences stripped.
    """
    content = content.strip()
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1 :]
    if content.rstrip().endswith("```"):
        content = content.rstrip()[:-3].rstrip()
    return content


def _build_import_hint(file_path: str) -> str:
    """Build a prompt section describing allowed local imports for a file.

    Args:
        file_path: Sanitized relative path of the file being generated.

    Returns:
        A formatted string to inject into the user prompt, or a
        no-imports constraint message if the file has no allowed local imports.
    """
    allowed = _resolve_import_graph(file_path)
    if not allowed:
        return (
            "\n## Import constraints\n"
            "This file has no allowed local imports. "
            "Do NOT import from any other local module."
        )
    return (
        "\n## Import constraints\n"
        f"This file may only import from these local modules: {allowed}\n"
        "Do NOT import from any other local module — this prevents circular dependencies."
    )


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


def developer_node(state: AgentState) -> dict:
    """LangGraph node: run the Developer agent with chunked file generation.

    Orchestrates a two-step process:
      1. Plan — ask the LLM which files to create (or derive from QA issues).
      2. Generate — call the LLM once per file with targeted instructions,
                    import constraints, and relevant QA feedback.

    On retry iterations, only files flagged by QA are regenerated; intact files
    are carried over from the previous state unchanged.

    Args:
        state: Current LangGraph agent state containing specs, prior code files,
               QA feedback, QA issues, and iteration count.

    Returns:
        A dict with updated ``code_files`` and ``reasoning_logs`` keys.
    """
    print("\n" + "=" * 60)
    print("👨‍💻 DEVELOPER AGENT — Generating Code")
    print("=" * 60)

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.2, max_tokens=8000)

    specs = state.get("specs", "")
    qa_feedback = state.get("qa_feedback", "")
    qa_issues = state.get("qa_issues", [])
    iteration = state.get("iteration", 0)

    # ── Determine file plan ──────────────────────────────────────────────────

    file_plan: list[str] | None = None
    issue_text = ""

    if qa_issues and iteration > 0:
        # Retry path: only regenerate files that QA flagged.
        issue_text = "\n".join(
            f"- [{iss['severity'].upper()}] {iss['file']}: {iss['description']}"
            for iss in qa_issues
        )
        plan_trace = f"Iteration {iteration}: regenerating files flagged by QA."
        print(f"\n[PLAN] {plan_trace}")

        flagged_files = {
            _sanitize_path(iss["file"])
            for iss in qa_issues
            if iss["file"] != "GENERAL"
        }
        has_general = any(iss["file"] == "GENERAL" for iss in qa_issues)

        if has_general or not flagged_files:
            file_plan = list(MANDATORY_FILES)
        else:
            file_plan = list(flagged_files)
            for mf in MANDATORY_FILES:
                if mf in flagged_files and mf not in file_plan:
                    file_plan.append(mf)

        print(f"[PLAN] Files to (re)generate: {file_plan}")

    elif qa_feedback and iteration > 0:
        # Legacy string feedback path: regenerate everything.
        plan_trace = (
            f"Iteration {iteration}: revising all code based on QA feedback."
        )
        print(f"\n[PLAN] {plan_trace}")
        issue_text = qa_feedback

    else:
        # First pass: ask the LLM for the full file plan.
        plan_trace = "First pass: planning files then generating code."
        print(f"\n[PLAN] {plan_trace}")

    # ── Step 1: LLM file plan (when not pre-determined) ─────────────────────

    if file_plan is None:
        print("[ACT]  Asking LLM for file plan …")
        try:
            plan_llm = llm.with_structured_output(FilePlan)
            plan_msg = f"## Specifications\n{specs}"
            if issue_text:
                plan_msg += f"\n\n## QA Issues to address\n{issue_text}"

            result: FilePlan = plan_llm.invoke(
                [
                    SystemMessage(content=PLAN_SYSTEM_PROMPT),
                    HumanMessage(content=plan_msg),
                ]
            )
            file_plan = [_sanitize_path(f) for f in result.files]
        except Exception as e:
            print(
                f"[ACT]  Structured plan failed ({type(e).__name__}), using mandatory list."
            )
            file_plan = list(MANDATORY_FILES)

        # Guarantee mandatory files are always included.
        for mf in MANDATORY_FILES:
            if not any(mf in fp for fp in file_plan):
                file_plan.append(mf)

    print(f"[ACT]  File plan ({len(file_plan)} files): {file_plan}")

    # ── Step 2: Generate each file individually ──────────────────────────────

    code_files: list[dict] = []
    existing_files: dict[str, dict] = {
        f["path"]: f for f in state.get("code_files", [])
    }

    for i, file_path in enumerate(file_plan, 1):
        file_path = _sanitize_path(file_path)
        language = _detect_language(file_path)

        print(f"[ACT]  Generating file {i}/{len(file_plan)}: {file_path} …")

        file_hint = _get_file_hint(file_path)
        import_hint = _build_import_hint(file_path)

        # Collect QA feedback relevant to this specific file.
        file_issues = ""
        if qa_issues and iteration > 0:
            relevant = [
                iss
                for iss in qa_issues
                if iss["file"] == file_path or iss["file"] == "GENERAL"
            ]
            if relevant:
                file_issues = (
                    "\n## QA issues to fix in THIS file:\n"
                    + "\n".join(
                        f"- [{iss['severity'].upper()}] {iss['description']}"
                        for iss in relevant
                    )
                )
        elif qa_feedback and iteration > 0:
            file_issues = f"\n## QA feedback (address what is relevant to this file):\n{qa_feedback}"

        user_msg = (
            f"## Application Specifications\n{specs}\n\n"
            f"## File to generate\nPath: `{file_path}`\n\n"
            f"## Instructions\n{file_hint}"
            f"{import_hint}"
            f"{file_issues}"
        )

        # Try structured output first, fall back to raw completion.
        content = ""
        try:
            content_llm = llm.with_structured_output(SingleFileContent)
            result_file = content_llm.invoke(
                [
                    SystemMessage(content=GENERATE_SYSTEM_PROMPT),
                    HumanMessage(content=user_msg),
                ]
            )
            content = result_file.content
        except Exception as e:
            print(
                f"         Structured failed ({type(e).__name__}), trying raw …"
            )
            try:
                response = llm.invoke(
                    [
                        SystemMessage(content=GENERATE_SYSTEM_PROMPT),
                        HumanMessage(content=user_msg),
                    ]
                )
                content = response.content
            except Exception as e2:
                print(
                    f"         Raw also failed ({type(e2).__name__}), skipping file."
                )
                continue

        content = _strip_markdown_fences(content)

        if content.strip():
            code_files.append(
                {
                    "path": file_path,
                    "content": content,
                    "language": language,
                }
            )
            print(f"         ✓ {len(content)} chars")
        else:
            print("         ✗ Empty content, skipping")

    # On retry merges: preserve old files that were not regenerated.
    if iteration > 0 and existing_files:
        regenerated_paths = {f["path"] for f in code_files}
        for path, old_file in existing_files.items():
            if path not in regenerated_paths:
                code_files.append(old_file)
                print(f"[ACT]  Kept existing file: {path}")

    # ── Reason phase ─────────────────────────────────────────────────────────

    file_list = (
        ", ".join(f["path"] for f in code_files) if code_files else "(none)"
    )
    reason_trace = f"Generated {len(code_files)} files: {file_list}"
    print(f"\n[REASON] {reason_trace}\n")

    return {
        "code_files": code_files,
        "reasoning_logs": [
            {"agent": "developer", "phase": "plan", "content": plan_trace},
            {
                "agent": "developer",
                "phase": "act",
                "content": f"Chunked generation: {len(code_files)} files produced.",
            },
            {"agent": "developer", "phase": "reason", "content": reason_trace},
        ],
    }
