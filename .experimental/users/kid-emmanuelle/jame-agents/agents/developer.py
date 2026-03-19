"""Developer Agent — Generates application code and tests.

Uses a two-step chunked approach to avoid token-limit and JSON-truncation
errors on large generations:

  Step 1  —  Ask the LLM for a *file plan* (list of paths to create).
  Step 2  —  Loop through those paths and call the LLM *once per file*
             to generate its content via structured output.
"""

from __future__ import annotations

import os

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from state import (
    AgentState,
    FilePlan,
    SingleFileContent,
    _sanitize_path,
)

# Files the Developer MUST include in every plan.
MANDATORY_FILES = [
    "backend/main.py",
    "backend/models.py",
    "backend/requirements.txt",
    "tests/test_main.py",
    "frontend/src/App.js",
    "frontend/package.json",
]

PLAN_SYSTEM_PROMPT = """\
You are the **Developer Agent** (planning phase) of a multi-agent software factory.

Given the application specifications, return the COMPLETE list of files that
need to be created.  Include every file required for a working project.

## MANDATORY files (always include these)
- backend/main.py       — FastAPI application entry point with all routes.
- backend/models.py     — Pydantic / SQLAlchemy data models.
- backend/requirements.txt — Python dependencies.
- tests/test_main.py    — Pytest tests covering every route.
- frontend/src/App.js   — React component with full UI.
- frontend/package.json — Node.js dependencies.

You may add MORE files if the specs warrant it (e.g. backend/database.py,
frontend/src/components/TaskList.js, etc.), but the mandatory files MUST
always be present.

Return ONLY the list of file paths — no explanations.
"""

GENERATE_SYSTEM_PROMPT = """\
You are the **Developer Agent** (code generation phase) of a multi-agent
software factory.

You will be given:
- Application specifications.
- The file path you must generate.
- Optionally, QA feedback to address.

## Rules
- Return ONLY the raw file content. No markdown fences, no explanations.
- The code must be complete, runnable, and production-quality.
- Include all imports, docstrings, and type hints (for Python).
- No placeholder comments like "TODO" or "pass" — implement everything.
- For Python files: use FastAPI, Pydantic, pytest.
- For JS/JSX files: use functional React components with hooks.
- For package.json / requirements.txt: list real, correct package names.
"""

FILE_CONTEXT = {
    "backend/main.py": (
        "Generate the FastAPI application entry point. Include ALL route "
        "handlers, CORS middleware (allow all origins for dev), and the "
        "uvicorn runner block."
    ),
    "backend/models.py": (
        "Generate all Pydantic models (request/response schemas) and, if "
        "needed, an in-memory data store (list or dict) used by the routes."
    ),
    "backend/requirements.txt": (
        "List all Python dependencies needed to run the backend and tests: "
        "fastapi, uvicorn, pydantic, pytest, httpx, etc."
    ),
    "tests/test_main.py": (
        "Generate pytest tests using FastAPI's TestClient. Cover every "
        "CRUD route with at least one happy-path and one edge-case test."
    ),
    "frontend/src/App.js": (
        "Generate a complete React functional component that provides a "
        "full UI for the application. Use fetch() to call the backend API "
        "at http://localhost:8000. Include state management with useState "
        "and useEffect hooks."
    ),
    "frontend/package.json": (
        "Generate a valid package.json with react, react-dom, and "
        "react-scripts as dependencies."
    ),
}


def _detect_language(path: str) -> str:
    """Infer programming language from file extension."""
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
    """Remove wrapping ```lang ... ``` fences if the LLM added them despite instructions."""
    content = content.strip()
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1:]
    if content.rstrip().endswith("```"):
        content = content.rstrip()[:-3].rstrip()
    return content


def developer_node(state: AgentState) -> dict:
    """LangGraph node: run the Developer agent with chunked generation."""

    print("\n" + "=" * 60)
    print("👨‍💻 DEVELOPER AGENT — Generating Code")
    print("=" * 60)

    llm = ChatOpenAI(
        model="deepseek-r1",
        temperature=0.2,
        max_tokens=8000,
        openai_api_key=os.getenv("SNOWFLAKE_API_KEY"),
        openai_api_base=os.getenv("SNOWFLAKE_API_BASE"),
    )

    specs = state.get("specs", "")
    qa_feedback = state.get("qa_feedback", "")
    qa_issues = state.get("qa_issues", [])
    iteration = state.get("iteration", 0)

    # ── Plan phase: decide which files to generate ──────────────
    if qa_issues and iteration > 0:
        # On retry, only regenerate files that QA flagged
        issue_text = "\n".join(
            f"- [{iss['severity'].upper()}] {iss['file']}: {iss['description']}"
            for iss in qa_issues
        )
        plan_trace = f"Iteration {iteration}: regenerating files flagged by QA."
        print(f"\n[PLAN] {plan_trace}")

        # Identify which files need rework
        flagged_files = {
            _sanitize_path(iss["file"])
            for iss in qa_issues
            if iss["file"] != "GENERAL"
        }
        # For GENERAL issues, regenerate all mandatory files
        has_general = any(iss["file"] == "GENERAL" for iss in qa_issues)

        if has_general or not flagged_files:
            file_plan = list(MANDATORY_FILES)
        else:
            # Keep existing good files, only redo flagged ones
            file_plan = list(flagged_files)
            # Ensure mandatory files are included if flagged
            for mf in MANDATORY_FILES:
                if mf in flagged_files and mf not in file_plan:
                    file_plan.append(mf)

        print(f"[PLAN] Files to (re)generate: {file_plan}")
    elif qa_feedback and iteration > 0:
        # Legacy string feedback path — regenerate everything
        plan_trace = f"Iteration {iteration}: revising all code based on QA feedback."
        print(f"\n[PLAN] {plan_trace}")
        issue_text = qa_feedback
        file_plan = None  # will be determined by LLM
    else:
        plan_trace = "First pass: planning files then generating code."
        print(f"\n[PLAN] {plan_trace}")
        issue_text = ""
        file_plan = None  # will be determined by LLM

    # ── Step 1: Get file plan from LLM (if not pre-determined) ──
    if file_plan is None:
        print("[ACT]  Asking LLM for file plan …")
        try:
            plan_llm = llm.with_structured_output(FilePlan)
            plan_msg = f"## Specifications\n{specs}"
            if issue_text:
                plan_msg += f"\n\n## QA Issues to address\n{issue_text}"

            result: FilePlan = plan_llm.invoke([
                SystemMessage(content=PLAN_SYSTEM_PROMPT),
                HumanMessage(content=plan_msg),
            ])
            file_plan = [_sanitize_path(f) for f in result.files]
        except Exception as e:
            print(f"[ACT]  Structured plan failed ({type(e).__name__}), using mandatory list.")
            file_plan = list(MANDATORY_FILES)

        # Ensure mandatory files are always included
        for mf in MANDATORY_FILES:
            if not any(mf in fp for fp in file_plan):
                file_plan.append(mf)

    print(f"[ACT]  File plan ({len(file_plan)} files): {file_plan}")

    # ── Step 2: Generate each file individually ─────────────────
    code_files: list[dict] = []
    existing_files = {f["path"]: f for f in state.get("code_files", [])}

    for i, file_path in enumerate(file_plan, 1):
        file_path = _sanitize_path(file_path)
        language = _detect_language(file_path)

        print(f"[ACT]  Generating file {i}/{len(file_plan)}: {file_path} …")

        # Build context for this specific file
        file_hint = FILE_CONTEXT.get(file_path, f"Generate the complete content for: {file_path}")

        # Gather relevant QA feedback for this file
        file_issues = ""
        if qa_issues and iteration > 0:
            relevant = [
                iss for iss in qa_issues
                if iss["file"] == file_path or iss["file"] == "GENERAL"
            ]
            if relevant:
                file_issues = "\n## QA issues to fix in THIS file:\n" + "\n".join(
                    f"- [{iss['severity'].upper()}] {iss['description']}"
                    for iss in relevant
                )
        elif qa_feedback and iteration > 0:
            file_issues = f"\n## QA feedback (address what's relevant to this file):\n{qa_feedback}"

        user_msg = (
            f"## Application Specifications\n{specs}\n\n"
            f"## File to generate\nPath: `{file_path}`\n\n"
            f"## Instructions\n{file_hint}"
            f"{file_issues}"
        )

        # Try structured output, fallback to raw
        content = ""
        try:
            content_llm = llm.with_structured_output(SingleFileContent)
            result = content_llm.invoke([
                SystemMessage(content=GENERATE_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ])
            content = result.content
        except Exception as e:
            print(f"         Structured failed ({type(e).__name__}), trying raw …")
            try:
                response = llm.invoke([
                    SystemMessage(content=GENERATE_SYSTEM_PROMPT),
                    HumanMessage(content=user_msg),
                ])
                content = response.content
            except Exception as e2:
                print(f"         Raw also failed ({type(e2).__name__}), skipping file.")
                continue

        content = _strip_markdown_fences(content)

        if content.strip():
            code_files.append({
                "path": file_path,
                "content": content,
                "language": language,
            })
            print(f"         ✓ {len(content)} chars")
        else:
            print(f"         ✗ Empty content, skipping")

    # On a retry, merge: keep old files that weren't re-generated
    if iteration > 0 and existing_files:
        regenerated_paths = {f["path"] for f in code_files}
        for path, old_file in existing_files.items():
            if path not in regenerated_paths:
                code_files.append(old_file)
                print(f"[ACT]  Kept existing file: {path}")

    # ── Reason phase ────────────────────────────────────────────
    file_list = ", ".join(f["path"] for f in code_files) if code_files else "(none)"
    reason_trace = f"Generated {len(code_files)} files: {file_list}"
    print(f"\n[REASON] {reason_trace}\n")

    return {
        "code_files": code_files,
        "reasoning_logs": [
            {"agent": "developer", "phase": "plan", "content": plan_trace},
            {"agent": "developer", "phase": "act", "content": f"Chunked generation: {len(code_files)} files produced."},
            {"agent": "developer", "phase": "reason", "content": reason_trace},
        ],
    }
