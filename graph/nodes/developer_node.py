"""Developer node — Generates application code and tests.

Implements an AIDLC-inspired four-phase workflow:

  Phase 1  —  Functional Design: extract domain entities, business rules,
              and component dependencies from the specs (technology-agnostic).
  Phase 2  —  File Planning: determine which files to generate, in strict
              dependency order.
  Phase 3  —  Code Generation: generate each file individually, passing
              already-generated dependency files as cross-reference context.
  Phase 4  —  Self-Validation: check consistency across all generated files
              before handing off to QA.

Architecture enforced across all generated projects:
  Router -> Service -> Repository/DB -> Model
"""

from __future__ import annotations

from pathlib import PurePosixPath

from cancel_token import raise_if_cancelled

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from graph.prompts.developer_prompts import (
    COMPONENT_HINT,
    FILE_CONTEXT,
    FUNCTIONAL_DESIGN_SYSTEM_PROMPT,
    GENERATE_SYSTEM_PROMPT,
    LIGHTWEIGHT_GENERATE_SYSTEM_PROMPT,
    LIGHTWEIGHT_PLAN_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
    ROUTER_HINT,
    SERVICE_HINT,
    VALIDATE_SYSTEM_PROMPT,
)
from graph.state import (
    AgentState,
    FilePlan,
    FunctionalDesign,
    SingleFileContent,
    ValidationResult,
    _sanitize_path,
)
from integrations.cortex import get_cortex_llm


# ---------------------------------------------------------------------------
# Constants
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

LAYER_ORDER: list[tuple[str, int]] = [
    ("backend/database.py", 0),
    ("backend/models.py", 1),
    ("backend/services/", 2),
    ("backend/routers/", 3),
    ("backend/main.py", 4),
    ("backend/requirements.txt", 5),
    ("frontend/package.json", 6),
    ("frontend/src/App.js", 7),
    ("frontend/src/components/", 8),
    ("tests/conftest.py", 9),
    ("tests/", 10),
]

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

DEPENDENCY_MAP: dict[str, list[str]] = {
    "backend/models.py": ["backend/database.py"],
    "backend/services/*.py": ["backend/models.py", "backend/database.py"],
    "backend/routers/*.py": [
        "backend/services/*.py",
        "backend/models.py",
        "backend/database.py",
    ],
    "backend/main.py": ["backend/routers/*.py", "backend/database.py"],
    "tests/conftest.py": [
        "backend/main.py",
        "backend/database.py",
        "backend/models.py",
    ],
    "tests/*.py": ["tests/conftest.py", "backend/main.py", "backend/models.py"],
    "frontend/src/App.js": [],
    "frontend/src/components/*": [],
}

LIGHTWEIGHT_SCOPES = ("function", "feature")

_EXT_TO_LANG = {
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

_SEVERITY_LABELS = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _log(level: str, phase: str, msg: str) -> None:
    """Print a structured log line with severity and phase."""
    print(f"[{phase}] [{_SEVERITY_LABELS.get(level, 'INFO')}] {msg}")


def _detect_language(path: str) -> str:
    """Infer the programming language from a file's extension."""
    return _EXT_TO_LANG.get(PurePosixPath(path).suffix, "text")


def _strip_markdown_fences(content: str) -> str:
    """Remove wrapping ``` fences if the LLM added them despite instructions."""
    content = content.strip()
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1 :]
    if content.rstrip().endswith("```"):
        content = content.rstrip()[:-3].rstrip()
    return content


def _match_wildcard(file_path: str, mapping: dict[str, list[str]]) -> list[str]:
    """Look up *file_path* in a dict whose keys may contain ``*`` wildcards."""
    if file_path in mapping:
        return mapping[file_path]
    for pattern, value in mapping.items():
        if "*" in pattern and file_path.startswith(pattern.split("*")[0]):
            return value
    return []


def _layer_priority(file_path: str) -> int:
    """Return the generation priority for *file_path* (lower = generated first)."""
    for pattern, priority in LAYER_ORDER:
        if file_path == pattern or file_path.startswith(pattern):
            return priority
    return 99


def _sort_by_layer(file_paths: list[str]) -> list[str]:
    """Sort file paths by architectural layer priority (bottom-up)."""
    return sorted(file_paths, key=_layer_priority)


def _get_file_hint(file_path: str) -> str:
    """Return the generation hint for a given file path."""
    if file_path in FILE_CONTEXT:
        return FILE_CONTEXT[file_path]
    if "routers/" in file_path:
        return ROUTER_HINT
    if "services/" in file_path:
        return SERVICE_HINT
    if "components/" in file_path:
        return COMPONENT_HINT
    return f"Generate the complete, production-quality content for: {file_path}"


def _build_import_hint(file_path: str) -> str:
    """Build a prompt section describing allowed local imports for a file."""
    allowed = _match_wildcard(file_path, IMPORT_GRAPH)
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


def _resolve_dependencies(file_path: str, generated: dict[str, dict]) -> str:
    """Build a cross-reference context section with already-generated dependency content.

    Instead of making the LLM guess function signatures and class names,
    we pass the actual content of dependency files so it can match
    interfaces exactly.
    """
    dep_patterns = _match_wildcard(file_path, DEPENDENCY_MAP)
    if not dep_patterns:
        return ""

    sections: list[str] = []
    for pattern in dep_patterns:
        if "*" in pattern:
            prefix = pattern.split("*")[0]
            matching = {
                p: f for p, f in generated.items() if p.startswith(prefix)
            }
        else:
            matching = (
                {pattern: generated[pattern]} if pattern in generated else {}
            )

        for dep_path, dep_file in matching.items():
            content = dep_file.get("content", "")
            if content:
                if len(content) > 4000:
                    content = content[:4000] + "\n# ... (truncated for brevity)"
                sections.append(f"### {dep_path}\n```\n{content}\n```")

    if not sections:
        return ""
    return (
        "\n## Already-generated dependency files (use EXACT interfaces)\n"
        "Match function signatures, class names, and field names exactly.\n\n"
        + "\n\n".join(sections)
    )


# ---------------------------------------------------------------------------
# LLM invocation (unified structured → raw fallback)
# ---------------------------------------------------------------------------


def _invoke_llm(
    llm: BaseChatModel,
    system_prompt: str,
    user_msg: str,
    *,
    schema: type | None = None,
    phase: str = "ACT",
) -> str | None:
    """Call the LLM with optional structured output, falling back to raw.

    Returns the extracted content string, or ``None`` on total failure.
    """
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ]
    if schema is not None:
        try:
            result = llm.with_structured_output(schema).invoke(messages)
        except Exception as e:
            _log(
                "medium",
                phase,
                f"Structured failed ({type(e).__name__}), trying raw …",
            )
        else:
            return result.content
    try:
        return llm.invoke(messages).content
    except Exception as e:
        _log("high", phase, f"LLM call failed ({type(e).__name__})")
    return None


# ---------------------------------------------------------------------------
# Single-file generation (shared by full-stack loop + auto-fix)
# ---------------------------------------------------------------------------


def _build_file_prompt(
    file_path: str,
    specs: str,
    functional_design: str,
    generated: dict[str, dict],
    file_issues: str = "",
) -> str:
    """Assemble the full user message for generating one file."""
    dep_context = _resolve_dependencies(file_path, generated)
    if dep_context:
        print(
            f"[ACT]    \u21b3 Injecting {dep_context.count('###')} "
            "dependency file(s) as context"
        )

    msg = f"## Application Specifications\n{specs}\n\n"
    if functional_design:
        msg += f"## Functional Design\n{functional_design}\n\n"
    msg += (
        f"## File to generate\nPath: `{file_path}`\n\n"
        f"## Instructions\n{_get_file_hint(file_path)}"
        f"{_build_import_hint(file_path)}"
        f"{dep_context}"
        f"{file_issues}"
    )
    return msg


def _generate_file(
    llm: BaseChatModel,
    file_path: str,
    specs: str,
    functional_design: str,
    generated: dict[str, dict],
    file_issues: str = "",
) -> dict | None:
    """Generate a single file. Returns its dict or ``None`` on failure."""
    user_msg = _build_file_prompt(
        file_path, specs, functional_design, generated, file_issues
    )
    content = _invoke_llm(
        llm,
        GENERATE_SYSTEM_PROMPT,
        user_msg,
        schema=SingleFileContent,
        phase="ACT",
    )
    if content is None:
        return None

    content = _strip_markdown_fences(content)
    if not content.strip():
        _log("high", "ACT", f"Empty content for {file_path}, skipping")
        return None

    print(f"[ACT]    \u2713 {len(content)} chars")
    return {
        "path": file_path,
        "content": content,
        "language": _detect_language(file_path),
    }


def _collect_file_issues(
    file_path: str,
    qa_issues: list[dict],
    qa_feedback: str,
    iteration: int,
) -> str:
    """Return QA feedback relevant to a specific file."""
    if qa_issues and iteration > 0:
        relevant = [
            iss for iss in qa_issues if iss["file"] in (file_path, "GENERAL")
        ]
        if relevant:
            return "\n## QA issues to fix in THIS file:\n" + "\n".join(
                f"- [{iss['severity'].upper()}] {iss['description']}"
                for iss in relevant
            )
    elif qa_feedback and iteration > 0:
        return (
            f"\n## QA feedback (address what is relevant to this file):\n"
            f"{qa_feedback}"
        )
    return ""


# ---------------------------------------------------------------------------
# Phase 1: Functional Design
# ---------------------------------------------------------------------------


def _run_functional_design(llm: BaseChatModel, specs: str) -> str:
    """Extract a technology-agnostic functional design from the specs."""
    print("\n[DESIGN] Extracting functional design from specifications \u2026")
    design = _invoke_llm(
        llm,
        FUNCTIONAL_DESIGN_SYSTEM_PROMPT,
        f"## Application Specifications\n{specs}",
        schema=FunctionalDesign,
        phase="DESIGN",
    )
    if design:
        print(
            f"[DESIGN] [INFO] Functional design extracted ({len(design)} chars)"
        )
        return design
    _log(
        "high",
        "DESIGN",
        "Functional design extraction failed. Proceeding without it.",
    )
    return ""


# ---------------------------------------------------------------------------
# Phase 2: File Planning
# ---------------------------------------------------------------------------


def _run_file_planning(
    llm: BaseChatModel,
    specs: str,
    functional_design: str,
    qa_issues: list[dict],
    qa_feedback: str,
    iteration: int,
) -> tuple[list[str], str]:
    """Determine which files to generate. Returns ``(file_plan, plan_trace)``."""
    file_plan: list[str] | None = None

    # Retry with structured QA issues — only regenerate flagged files.
    if qa_issues and iteration > 0:
        plan_trace = f"Iteration {iteration}: regenerating files flagged by QA."
        print(f"\n[PLAN] {plan_trace}")

        flagged = {
            _sanitize_path(iss["file"])
            for iss in qa_issues
            if iss["file"] != "GENERAL"
        }
        has_general = any(iss["file"] == "GENERAL" for iss in qa_issues)
        file_plan = (
            list(MANDATORY_FILES)
            if (has_general or not flagged)
            else list(flagged)
        )
        print(f"[PLAN] Files to (re)generate: {file_plan}")

    # Retry with legacy string feedback — regenerate everything.
    elif qa_feedback and iteration > 0:
        plan_trace = (
            f"Iteration {iteration}: revising all code based on QA feedback."
        )
        print(f"\n[PLAN] {plan_trace}")

    # First pass — ask the LLM.
    else:
        plan_trace = "First pass: planning files then generating code."
        print(f"\n[PLAN] {plan_trace}")

    # Ask LLM for plan when not pre-determined by QA issues.
    if file_plan is None:
        print("[PLAN] Asking LLM for file plan \u2026")
        plan_msg = f"## Specifications\n{specs}"
        if functional_design:
            plan_msg += f"\n\n## Functional Design\n{functional_design}"
        if qa_feedback and iteration > 0:
            plan_msg += f"\n\n## QA Issues to address\n{qa_feedback}"

        try:
            plan_llm = llm.with_structured_output(FilePlan)
            result: FilePlan = plan_llm.invoke(
                [
                    SystemMessage(content=PLAN_SYSTEM_PROMPT),
                    HumanMessage(content=plan_msg),
                ]
            )
            file_plan = [_sanitize_path(f) for f in result.files]
        except Exception as e:
            _log(
                "medium",
                "PLAN",
                f"Structured plan failed ({type(e).__name__}), using mandatory list.",
            )
            file_plan = list(MANDATORY_FILES)

        # Guarantee mandatory files are always present.
        for mf in MANDATORY_FILES:
            if not any(mf in fp for fp in file_plan):
                file_plan.append(mf)

    file_plan = _sort_by_layer(file_plan)
    print(
        f"[PLAN] File plan ({len(file_plan)} files, dependency-ordered): {file_plan}"
    )
    return file_plan, plan_trace


# ---------------------------------------------------------------------------
# Phase 3: Code Generation
# ---------------------------------------------------------------------------


def _run_code_generation(
    llm: BaseChatModel,
    file_plan: list[str],
    specs: str,
    functional_design: str,
    existing_files: dict[str, dict],
    qa_issues: list[dict],
    qa_feedback: str,
    iteration: int,
) -> tuple[list[dict], dict[str, dict]]:
    """Generate all planned files. Returns ``(code_files, generated_map)``."""
    code_files: list[dict] = []
    generated: dict[str, dict] = {}

    # Pre-populate context from prior iteration so dependencies are available.
    if iteration > 0 and existing_files:
        generated.update(existing_files)

    for i, file_path in enumerate(file_plan, 1):
        file_path = _sanitize_path(file_path)
        print(
            f"\n[ACT]  Generating file {i}/{len(file_plan)}: {file_path} \u2026"
        )

        file_issues = _collect_file_issues(
            file_path, qa_issues, qa_feedback, iteration
        )
        result = _generate_file(
            llm, file_path, specs, functional_design, generated, file_issues
        )
        if result:
            code_files.append(result)
            generated[file_path] = result

    # Preserve existing files that were not regenerated on retry.
    if iteration > 0 and existing_files:
        regenerated_paths = {f["path"] for f in code_files}
        for path, old_file in existing_files.items():
            if path not in regenerated_paths:
                code_files.append(old_file)
                print(f"[ACT]  Kept existing file: {path}")

    return code_files, generated


# ---------------------------------------------------------------------------
# Phase 4: Self-Validation
# ---------------------------------------------------------------------------


def _run_self_validation(
    llm: BaseChatModel, code_files: list[dict]
) -> list[dict]:
    """Check consistency across all generated files before QA.

    Returns a list of issue dicts (empty if validation passed).
    """
    if not code_files:
        return []

    print("\n[VALIDATE] Running self-validation on generated files \u2026")

    file_summary = []
    for f in code_files:
        content = f.get("content", "")
        if len(content) > 3000:
            content = content[:3000] + "\n# ... (truncated)"
        file_summary.append(
            f"### {f['path']}\n```{f.get('language', '')}\n{content}\n```"
        )

    try:
        val_llm = llm.with_structured_output(ValidationResult)
        result: ValidationResult = val_llm.invoke(
            [
                SystemMessage(content=VALIDATE_SYSTEM_PROMPT),
                HumanMessage(
                    content="## Generated Files\n\n" + "\n\n".join(file_summary)
                ),
            ]
        )
        if result.passed:
            print("[VALIDATE] [INFO] \u2713 All consistency checks passed")
            return []

        issues = [
            {
                "file": iss.file,
                "severity": iss.severity,
                "description": iss.description,
            }
            for iss in result.issues
        ]
        for iss in issues:
            _log(
                iss["severity"]
                if iss["severity"] in _SEVERITY_LABELS
                else "medium",
                "VALIDATE",
                f"{iss['file']}: {iss['description']}",
            )
        print(f"[VALIDATE] [INFO] Found {len(issues)} consistency issues")
    except Exception as e:
        _log(
            "medium",
            "VALIDATE",
            f"Self-validation failed ({type(e).__name__}). Skipping.",
        )
        return []
    else:
        return issues


def _run_auto_fix(
    llm: BaseChatModel,
    code_files: list[dict],
    generated: dict[str, dict],
    validation_issues: list[dict],
    specs: str,
    functional_design: str,
) -> None:
    """Auto-fix files with critical/major validation issues. Mutates *code_files* in place."""
    critical_files = {
        iss["file"]
        for iss in validation_issues
        if iss["severity"] in ("critical", "major")
    }
    if not critical_files:
        return

    print(
        f"\n[VALIDATE] Auto-fixing {len(critical_files)} file(s) "
        "with critical/major issues \u2026"
    )
    fix_text = "\n".join(
        f"- [{iss['severity'].upper()}] {iss['file']}: {iss['description']}"
        for iss in validation_issues
    )

    for file_path in critical_files:
        file_idx = next(
            (i for i, f in enumerate(code_files) if f["path"] == file_path),
            None,
        )
        if file_idx is None:
            continue

        print(f"[VALIDATE] Regenerating: {file_path} \u2026")
        result = _generate_file(
            llm,
            file_path,
            specs,
            functional_design,
            generated,
            file_issues=f"\n## Consistency issues to fix in THIS file:\n{fix_text}",
        )
        if result:
            code_files[file_idx] = result
            generated[file_path] = result


# ---------------------------------------------------------------------------
# Lightweight path (function / feature scope)
# ---------------------------------------------------------------------------


def _resolve_all_generated(generated: dict[str, dict]) -> str:
    """Build cross-reference from *all* already-generated files (lightweight path)."""
    if not generated:
        return ""
    parts = [
        f"### {p}\n```\n{f['content']}\n```"
        for p, f in generated.items()
        if f.get("content")
    ]
    if not parts:
        return ""
    return (
        "\n## Already-generated files (use EXACT interfaces)\n"
        + "\n\n".join(parts)
    )


def _run_lightweight(llm: BaseChatModel, scope: str, specs: str) -> list[dict]:
    """Generate code for a function- or feature-level request.

    Skips the full-stack pipeline entirely — produces only the minimal files
    the user actually needs (e.g. ``fibonacci.py`` + ``test_fibonacci.py``).
    """
    print(f"\n[PLAN] Lightweight scope ({scope}) \u2014 minimal file set")

    plan_prompt = LIGHTWEIGHT_PLAN_SYSTEM_PROMPT.format(scope=scope)
    try:
        plan_llm = llm.with_structured_output(FilePlan)
        result: FilePlan = plan_llm.invoke(
            [
                SystemMessage(content=plan_prompt),
                HumanMessage(content=f"## Specifications\n{specs}"),
            ]
        )
        file_plan = [_sanitize_path(f) for f in result.files]
    except Exception as e:
        _log(
            "medium",
            "PLAN",
            f"Structured plan failed ({type(e).__name__}), using fallback.",
        )
        file_plan = []

    if not file_plan:
        file_plan = ["solution.py", "test_solution.py"]
    print(f"[PLAN] Files: {file_plan}")

    gen_prompt = LIGHTWEIGHT_GENERATE_SYSTEM_PROMPT.format(scope=scope)
    code_files: list[dict] = []
    generated: dict[str, dict] = {}

    for i, file_path in enumerate(file_plan, 1):
        file_path = _sanitize_path(file_path)
        print(
            f"\n[ACT]  Generating file {i}/{len(file_plan)}: {file_path} \u2026"
        )

        user_msg = (
            f"## Specifications\n{specs}\n\n"
            f"## File to generate\nPath: `{file_path}`"
            f"{_resolve_all_generated(generated)}"
        )
        content = _invoke_llm(
            llm,
            gen_prompt,
            user_msg,
            schema=SingleFileContent,
            phase="ACT",
        )
        if content is None:
            continue

        content = _strip_markdown_fences(content)
        if content.strip():
            file_dict = {
                "path": file_path,
                "content": content,
                "language": _detect_language(file_path),
            }
            code_files.append(file_dict)
            generated[file_path] = file_dict
            print(f"[ACT]    \u2713 {len(content)} chars")
        else:
            _log("high", "ACT", f"Empty content for {file_path}, skipping")

    return code_files


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


def developer_node(state: AgentState) -> dict:
    """LangGraph node: run the Developer agent with AIDLC-inspired workflow.

    Orchestrates four phases — Design, Plan, Generate, Validate — then
    returns updated state with ``functional_design``, ``code_files``, and
    ``reasoning_logs``.
    """
    raise_if_cancelled()
    print("\n" + "=" * 60)
    print(
        "\U0001f468\u200d\U0001f4bb DEVELOPER AGENT \u2014 AIDLC-Inspired Workflow"
    )
    print("=" * 60)

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.2, max_tokens=8000)

    specs = state.get("specs", "")
    scope = state.get("scope", "system")
    qa_feedback = state.get("qa_feedback", "")
    qa_issues = state.get("qa_issues", [])
    iteration = state.get("iteration", 0)

    # ── Lightweight path: function / feature scope ───────────────
    if scope in LIGHTWEIGHT_SCOPES and iteration == 0:
        print(
            f"\n\u26a1 Lightweight mode ({scope}) \u2014 skipping full-stack pipeline"
        )
        code_files = _run_lightweight(llm, scope, specs)
        file_list = (
            ", ".join(f["path"] for f in code_files) if code_files else "(none)"
        )
        reason = f"Lightweight ({scope}): generated {len(code_files)} file(s): {file_list}"
        print(f"\n[REASON] {reason}\n")
        return {
            "functional_design": "",
            "code_files": code_files,
            "reasoning_logs": [
                {
                    "agent": "developer",
                    "phase": "plan",
                    "content": f"Lightweight scope ({scope}) \u2014 minimal file set.",
                },
                {
                    "agent": "developer",
                    "phase": "act",
                    "content": f"Generated {len(code_files)} file(s).",
                },
                {"agent": "developer", "phase": "reason", "content": reason},
            ],
        }

    # ── Phase 1: Functional Design ───────────────────────────────
    functional_design = state.get("functional_design", "")
    if iteration == 0:
        functional_design = _run_functional_design(llm, specs)
        design_trace = (
            f"Extracted functional design ({len(functional_design)} chars): "
            "domain entities, business rules, API endpoints, NFR considerations."
        )
    else:
        design_trace = "Reusing functional design from previous iteration."
        print(f"\n[DESIGN] {design_trace}")

    # ── Phase 2: File Planning ───────────────────────────────────
    file_plan, plan_trace = _run_file_planning(
        llm,
        specs,
        functional_design,
        qa_issues,
        qa_feedback,
        iteration,
    )

    # ── Phase 3: Code Generation ─────────────────────────────────
    existing_files = {f["path"]: f for f in state.get("code_files", [])}
    code_files, generated = _run_code_generation(
        llm,
        file_plan,
        specs,
        functional_design,
        existing_files,
        qa_issues,
        qa_feedback,
        iteration,
    )

    # ── Phase 4: Self-Validation ─────────────────────────────────
    validation_issues = _run_self_validation(llm, code_files)
    if validation_issues:
        _run_auto_fix(
            llm,
            code_files,
            generated,
            validation_issues,
            specs,
            functional_design,
        )

    # ── Reasoning trace ──────────────────────────────────────────
    file_list = (
        ", ".join(f["path"] for f in code_files) if code_files else "(none)"
    )
    reason_trace = f"Generated {len(code_files)} files: {file_list}"
    if validation_issues:
        reason_trace += (
            f" | Self-validation found {len(validation_issues)} issues "
            "(auto-fix attempted)"
        )
    print(f"\n[REASON] {reason_trace}\n")

    return {
        "functional_design": functional_design,
        "code_files": code_files,
        "reasoning_logs": [
            {"agent": "developer", "phase": "design", "content": design_trace},
            {"agent": "developer", "phase": "plan", "content": plan_trace},
            {
                "agent": "developer",
                "phase": "act",
                "content": f"Chunked generation: {len(code_files)} files produced (dependency-ordered).",
            },
            {
                "agent": "developer",
                "phase": "validate",
                "content": (
                    f"Self-validation: {len(validation_issues)} issues found."
                    if validation_issues
                    else "Self-validation: all checks passed."
                ),
            },
            {"agent": "developer", "phase": "reason", "content": reason_trace},
        ],
    }
