"""Developer Agent — Generates application code and tests.

Uses a two-step chunked approach to avoid token-limit and JSON-truncation
errors on large generations:

  Step 1  —  Ask the LLM for a *file plan* (list of paths to create).
  Step 2  —  Loop through those paths and call the LLM *once per file*
             to generate its content via structured output.

Architecture enforced across all generated projects:
  Router → Service → Repository/DB → Model
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
# Import graph — prevents circular dependencies across generated files
# Each key is a file pattern; value is the list of local modules it may import.
# ---------------------------------------------------------------------------

IMPORT_GRAPH: dict[str, list[str]] = {
    "backend/main.py":          ["backend/routers/*", "backend/database.py"],
    "backend/routers/*.py":     ["backend/services/*.py", "backend/models.py", "backend/database.py"],
    "backend/services/*.py":    ["backend/models.py", "backend/database.py"],
    "backend/models.py":        [],
    "backend/database.py":      ["backend/models.py"],
    "tests/conftest.py":        ["backend/main.py", "backend/database.py"],
    "tests/*.py":               ["backend/main.py", "backend/database.py", "tests/conftest.py"],
    "frontend/src/components/*":["(props only — no cross-component imports)"],
    "frontend/src/App.js":      ["frontend/src/components/*"],
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


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = """\
You are the **Developer Agent** (planning phase) of a multi-agent software factory.

Given the application specifications, return the COMPLETE list of files needed
for a production-quality, maintainable project.

## MANDATORY files (always include ALL of these)
- backend/main.py            — FastAPI app: router registrations, middleware, lifespan.
- backend/routers/<name>.py  — One router file per resource (e.g. routers/tasks.py).
- backend/services/<name>.py — Business logic layer, one file per resource.
- backend/models.py          — Pydantic request/response schemas + ORM models.
- backend/database.py        — DB engine, session factory, Base class, get_db() dependency.
- backend/requirements.txt   — Pinned Python dependencies (exact versions with ==).
- tests/conftest.py          — pytest fixtures: TestClient, in-memory DB override, seed data.
- tests/test_<name>.py       — One test file per router, covering ALL routes.
- frontend/src/App.js        — Root React component: routing, global state only.
- frontend/src/components/   — At least one component file per UI feature.
- frontend/package.json      — Pinned Node.js dependencies (exact versions).

## Architecture rule
Enforce a strict layered pattern: Router → Service → Repository/DB.
- Routers: HTTP parsing and delegation ONLY. No business logic.
- Services: ALL business rules, validation, error handling.
- Models/DB: schemas and persistence ONLY.

## Dependency graph rule
For each file, verify its import graph is acyclic before adding it to the plan.
No router may import another router. No service may import a router.

Return ONLY the list of file paths — no explanations.
"""

GENERATE_SYSTEM_PROMPT = """\
You are the **Developer Agent** (code generation phase) of a multi-agent software factory.

You will receive: the file path to generate, the full application specifications,
allowed local imports for this file, and optionally QA feedback to address.

## Output rules
- Return ONLY raw file content. No markdown fences, no preamble, no explanation.
- Code must be complete, runnable, and production-quality.
- No TODOs, no `pass` stubs, no placeholder comments — implement everything.

## Architecture & layering
- Routers: declare routes, parse HTTP inputs, delegate ALL logic to the service layer.
  Never put SQL, business rules, or computation inside a router function.
- Services: implement ALL business rules (validation, computation, state transitions).
  Raise HTTPException with meaningful messages for all error paths.
  Never import from a router module.
- Models/DB: data schemas and persistence only.
- Use FastAPI `Depends` for DB session and service instance injection.

## Business logic requirements
- Implement REAL logic — no placeholder returns or empty functions.
- Validate inputs beyond Pydantic: referential integrity, business constraints
  (non-negative amounts, non-empty strings, date ordering, unique fields).
- Return semantically correct HTTP status codes:
    201 for resource creation.
    204 for deletion with no body.
    404 with a clear message when a resource is not found.
    409 for conflicts (duplicate unique fields).
    422 is handled automatically by Pydantic — do not duplicate it.
- Handle ALL foreseeable error paths explicitly with HTTPException.

## Docstrings & comments
- Every Python module: a one-line module docstring describing its responsibility.
- Every function/method: a Google-style docstring with Args, Returns, and Raises sections.
- Add inline comments ONLY for non-obvious logic (algorithms, edge-case handling).
  Do NOT comment self-explanatory code such as `# return the result`.
- React components: a JSDoc comment above each component describing its props and purpose.

## Dependencies & imports
- Use ONLY packages listed in requirements.txt (Python) or package.json (JS).
- Pin exact versions in requirements.txt: fastapi==0.111.0, NOT fastapi>=0.100.
- List every package you explicitly import, including transitive ones (httpx, sqlalchemy…).
- Python import order: stdlib → third-party → local. One blank line between each group.
- Respect the allowed imports provided for this file. Never create circular imports.

## Testing rules (test files only)
- Use pytest + FastAPI TestClient. Import the app from backend.main.
- Organise with one test class per route group.
- For EACH route write:
    1. One happy-path test asserting status code AND response body.
    2. One not-found / resource-missing test (where applicable).
    3. One invalid-input test (missing field, wrong type, boundary value).
    4. One business-logic edge-case test specific to the feature.
- Use descriptive test names: test_create_task_returns_201_with_valid_payload.
- Never share mutable state between tests; use fixtures for isolation.

## React / frontend rules
- Functional components with hooks only — no class components.
- Separate concerns: one component per UI feature, lift state only when shared.
- Handle loading, error, and empty states explicitly in every data-fetching component.
- API base URL must come from env: process.env.REACT_APP_API_URL ?? 'http://localhost:8000'.
- Every useEffect that reads a variable must list it in the dependency array.
  An empty array [] is only valid when the effect truly runs once on mount.
- Never fetch data directly in render — always inside useEffect or an event handler.

## Language-specific rules (targeted, non-obvious only)
- Python async consistency: if any route is `async def`, ALL service methods it calls
  must also be `async def`. Never call a blocking synchronous function from an async route
  without wrapping it in asyncio.to_thread().
- Python mutable defaults: never use mutable default arguments (e.g. def f(items=[])).
  Use None and assign the default inside the function body.
"""

# ---------------------------------------------------------------------------
# Per-file generation hints
# ---------------------------------------------------------------------------

FILE_CONTEXT: dict[str, str] = {
    "backend/main.py": (
        "Generate the FastAPI application entry point. "
        "Register all routers with appropriate URL prefixes and OpenAPI tags. "
        "Add CORS middleware (allow all origins for dev). "
        "Use a lifespan context manager for startup/shutdown (create DB tables on startup). "
        "Include the uvicorn runner block. "
        "Do NOT put any route logic or business logic here."
    ),
    "backend/database.py": (
        "Generate the SQLAlchemy setup: engine (SQLite for dev), SessionLocal factory, "
        "and declarative Base. "
        "Provide a get_db() generator function suitable for FastAPI Depends injection. "
        "Use SQLite with a file-based URL for dev so tests can override with :memory:."
    ),
    "backend/models.py": (
        "Generate all Pydantic schemas (Base, Create, Update, Response) "
        "and SQLAlchemy ORM models. "
        "Keep request schemas and response schemas strictly separate. "
        "Add field-level validators (field_validator) for business constraints "
        "(e.g. non-empty strings, non-negative numbers, valid enums). "
        "ORM models must inherit from the SQLAlchemy Base defined in database.py."
    ),
    "backend/requirements.txt": (
        "List ALL Python dependencies with PINNED exact versions (== operator, not >=). "
        "Required packages: fastapi, uvicorn[standard], pydantic, sqlalchemy, "
        "pytest, pytest-cov, httpx, anyio. "
        "Add others only if the specs explicitly require them. "
        "One package per line. No comments."
    ),
    "tests/conftest.py": (
        "Generate shared pytest fixtures: "
        "- `client`: TestClient wrapping the FastAPI app, overriding get_db() "
        "  to use an isolated in-memory SQLite DB created fresh for each test session. "
        "- `seed_<resource>`: one fixture per resource that inserts minimal valid test data "
        "  and returns the created object so tests can reference its ID. "
        "Fixtures must be fully isolated — no shared mutable state between tests."
    ),
    "tests/test_main.py": (
        "Generate the full pytest test suite for the main API routes. "
        "Use one class per route group (e.g. class TestCreateTask, class TestListTasks). "
        "For each route include: happy-path, not-found, invalid-input, and edge-case tests. "
        "Assert both the HTTP status code and the JSON response body shape in every test. "
        "Use descriptive names: test_create_task_returns_201_with_valid_payload."
    ),
    "frontend/src/App.js": (
        "Generate the root React functional component. "
        "Set up client-side routing if multiple views are needed (use react-router-dom). "
        "Manage only truly global state here (auth, theme, notifications). "
        "Delegate all feature-specific state to child components."
    ),
    "frontend/package.json": (
        "Generate a valid package.json with PINNED exact versions (no ^ or ~) for: "
        "react, react-dom, react-scripts. "
        "Add react-router-dom if multiple views are required by the specs. "
        "Set 'proxy': 'http://localhost:8000' to proxy API calls in dev. "
        "Include a 'scripts' section with start, build, and test."
    ),
}

# Generic hints for router/service files — interpolated at generation time.
_ROUTER_HINT = (
    "Generate the FastAPI router for this resource. "
    "Each endpoint must: validate the HTTP input via Pydantic, call the corresponding "
    "service method, and return the correct HTTP status code (201/204/404/409 as appropriate). "
    "No business logic, no DB calls — delegate everything to the service layer."
)

_SERVICE_HINT = (
    "Generate the service layer for this resource. "
    "Implement ALL business rules: input validation beyond Pydantic, "
    "state transitions, computed fields, and referential integrity checks. "
    "Raise HTTPException with clear messages for every error path. "
    "Call SQLAlchemy ORM methods for DB operations; never write raw SQL strings."
)

_COMPONENT_HINT = (
    "Generate a React functional component with a JSDoc header. "
    "Handle three UI states explicitly: loading (spinner or skeleton), "
    "error (user-friendly message + retry option), and empty (helpful placeholder text). "
    "Fetch data inside useEffect; list all dependencies in the dependency array. "
    "Expose mutations via callback props, not internal fetch calls."
)


def _get_file_hint(file_path: str) -> str:
    """Return the generation hint for a given file path.

    Checks the static FILE_CONTEXT dict first, then falls back to pattern-based hints
    for routers, services, and React components.

    Args:
        file_path: Sanitized relative path of the file being generated.

    Returns:
        A string instruction describing what the file should contain.
    """
    if file_path in FILE_CONTEXT:
        return FILE_CONTEXT[file_path]
    if "routers/" in file_path:
        return _ROUTER_HINT
    if "services/" in file_path:
        return _SERVICE_HINT
    if "components/" in file_path:
        return _COMPONENT_HINT
    return f"Generate the complete, production-quality content for: {file_path}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_language(path: str) -> str:
    """Infer the programming language from a file's extension.

    Args:
        path: Relative file path.

    Returns:
        A lowercase language string suitable for syntax highlighting.
    """
    ext_map = {
        ".py":   "python",
        ".js":   "javascript",
        ".jsx":  "javascript",
        ".ts":   "typescript",
        ".tsx":  "typescript",
        ".json": "json",
        ".yaml": "yaml",
        ".yml":  "yaml",
        ".txt":  "text",
        ".md":   "markdown",
        ".html": "html",
        ".css":  "css",
    }
    for ext, lang in ext_map.items():
        if path.endswith(ext):
            return lang
    return "text"


def _strip_markdown_fences(content: str) -> str:
    """Remove wrapping ```lang ... ``` fences if the LLM added them despite instructions.

    Args:
        content: Raw string returned by the LLM.

    Returns:
        Content with opening and closing fences stripped.
    """
    content = content.strip()
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1:]
    if content.rstrip().endswith("```"):
        content = content.rstrip()[:-3].rstrip()
    return content


def _build_import_hint(file_path: str) -> str:
    """Build a prompt section describing allowed local imports for a file.

    Prevents the LLM from creating circular dependencies by explicitly listing
    what this file is allowed to import from the local codebase.

    Args:
        file_path: Sanitized relative path of the file being generated.

    Returns:
        A formatted string to inject into the user prompt, or empty string
        if no constraints apply.
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
        A dict with updated `code_files` and `reasoning_logs` keys.
    """
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

    specs       = state.get("specs", "")
    qa_feedback = state.get("qa_feedback", "")
    qa_issues   = state.get("qa_issues", [])
    iteration   = state.get("iteration", 0)

    # ── Determine file plan ──────────────────────────────────────────────────

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
        plan_trace = f"Iteration {iteration}: revising all code based on QA feedback."
        print(f"\n[PLAN] {plan_trace}")
        issue_text = qa_feedback
        file_plan  = None

    else:
        # First pass: ask the LLM for the full file plan.
        plan_trace = "First pass: planning files then generating code."
        print(f"\n[PLAN] {plan_trace}")
        issue_text = ""
        file_plan  = None

    # ── Step 1: LLM file plan (when not pre-determined) ─────────────────────

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

        # Guarantee mandatory files are always included.
        for mf in MANDATORY_FILES:
            if not any(mf in fp for fp in file_plan):
                file_plan.append(mf)

    print(f"[ACT]  File plan ({len(file_plan)} files): {file_plan}")

    # ── Step 2: Generate each file individually ──────────────────────────────

    code_files: list[dict]       = []
    existing_files: dict[str, dict] = {f["path"]: f for f in state.get("code_files", [])}

    for i, file_path in enumerate(file_plan, 1):
        file_path = _sanitize_path(file_path)
        language  = _detect_language(file_path)

        print(f"[ACT]  Generating file {i}/{len(file_plan)}: {file_path} …")

        # Build contextual hint for this specific file.
        file_hint   = _get_file_hint(file_path)
        import_hint = _build_import_hint(file_path)

        # Collect QA feedback relevant to this file.
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
            file_issues = (
                f"\n## QA feedback (address what is relevant to this file):\n{qa_feedback}"
            )
        
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
            result      = content_llm.invoke([
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
                "path":     file_path,
                "content":  content,
                "language": language,
            })
            print(f"         ✓ {len(content)} chars")
        else:
            print(f"         ✗ Empty content, skipping")

    # On retry, merge: preserve old files that were not regenerated.
    if iteration > 0 and existing_files:
        regenerated_paths = {f["path"] for f in code_files}
        for path, old_file in existing_files.items():
            if path not in regenerated_paths:
                code_files.append(old_file)
                print(f"[ACT]  Kept existing file: {path}")

    # ── Reason phase ─────────────────────────────────────────────────────────

    file_list    = ", ".join(f["path"] for f in code_files) if code_files else "(none)"
    reason_trace = f"Generated {len(code_files)} files: {file_list}"
    print(f"\n[REASON] {reason_trace}\n")

    return {
        "code_files": code_files,
        "reasoning_logs": [
            {"agent": "developer", "phase": "plan",   "content": plan_trace},
            {"agent": "developer", "phase": "act",    "content": f"Chunked generation: {len(code_files)} files produced."},
            {"agent": "developer", "phase": "reason", "content": reason_trace},
        ],
    }
