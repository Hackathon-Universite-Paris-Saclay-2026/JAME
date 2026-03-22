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
  Router → Service → Repository/DB → Model
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from graph.prompts.developer_prompts import (
    COMPONENT_HINT,
    FILE_CONTEXT,
    FUNCTIONAL_DESIGN_SYSTEM_PROMPT,
    GENERATE_SYSTEM_PROMPT,
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
# AIDLC-inspired: Architectural layer ordering for dependency-aware generation.
# Files are generated bottom-up so that each file can reference the actual
# interfaces of its dependencies (already generated in earlier layers).
# ---------------------------------------------------------------------------

LAYER_ORDER: list[tuple[str, int]] = [
    # (path pattern, priority — lower = generated first)
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


def _layer_priority(file_path: str) -> int:
    """Return the generation priority for a file based on its architectural layer.

    Lower values are generated first so their content is available as
    cross-reference context for higher layers.

    Args:
        file_path: Sanitized relative path of the file.

    Returns:
        An integer priority (0 = first, 99 = fallback).
    """
    for pattern, priority in LAYER_ORDER:
        if file_path == pattern or file_path.startswith(pattern):
            return priority
    return 99


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

# ---------------------------------------------------------------------------
# AIDLC-inspired: Dependency mapping for cross-file context.
# When generating file X, include the content of its dependency files so the
# LLM uses actual interfaces instead of guessing.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Error severity (AIDLC-inspired)
# ---------------------------------------------------------------------------

_SEVERITY_LABELS = {
    "critical": "CRITICAL",  # Generation cannot continue
    "high": "HIGH",  # Current file blocked
    "medium": "MEDIUM",  # Partial progress possible
    "low": "LOW",  # Non-blocking warning
}


def _log(level: str, phase: str, msg: str) -> None:
    """Print a structured log message with severity and phase.

    Args:
        level: One of 'critical', 'high', 'medium', 'low', or 'info'.
        phase: AIDLC phase label (DESIGN, PLAN, ACT, VALIDATE, REASON).
        msg: Human-readable log message.
    """
    label = _SEVERITY_LABELS.get(level, "INFO")
    print(f"[{phase}] [{label}] {msg}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _resolve_dependencies(file_path: str, generated: dict[str, dict]) -> str:
    """Build a cross-reference context section with already-generated dependency content.

    AIDLC-inspired: Instead of making the LLM guess function signatures and
    class names, we pass the actual content of dependency files so it can
    match interfaces exactly.

    Args:
        file_path: Sanitized relative path of the file being generated.
        generated: Dict mapping path → CodeFile dict for already-generated files.

    Returns:
        A formatted string with dependency file contents, or empty string
        if no dependencies are available.
    """
    dep_patterns: list[str] = []
    for pattern, deps in DEPENDENCY_MAP.items():
        if file_path == pattern:
            dep_patterns = deps
            break
        if "*" in pattern:
            prefix = pattern.split("*")[0]
            if file_path.startswith(prefix):
                dep_patterns = deps
                break

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
                # Truncate very large files to keep the prompt manageable
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


def _sort_by_layer(file_paths: list[str]) -> list[str]:
    """Sort file paths by architectural layer priority.

    AIDLC-inspired: Files are generated bottom-up (models before services,
    services before routers) so cross-file context is available.

    Args:
        file_paths: Unsorted list of relative file paths.

    Returns:
        The same paths sorted by generation priority (lowest first).
    """
    return sorted(file_paths, key=_layer_priority)


# ---------------------------------------------------------------------------
# Phase 1: Functional Design (AIDLC-inspired)
# ---------------------------------------------------------------------------


def _run_functional_design(llm: BaseChatModel, specs: str) -> str:
    """Extract a technology-agnostic functional design from the specs.

    Identifies domain entities, business rules, API endpoints, component
    dependencies, and NFR considerations before any code is generated.

    Args:
        llm: LangChain LLM instance.
        specs: Application specifications from the Architect agent.

    Returns:
        The functional design analysis as a string, or empty string on failure.
    """
    print("\n[DESIGN] Extracting functional design from specifications …")
    try:
        design_llm = llm.with_structured_output(FunctionalDesign)
        result: FunctionalDesign = design_llm.invoke(
            [
                SystemMessage(content=FUNCTIONAL_DESIGN_SYSTEM_PROMPT),
                HumanMessage(content=f"## Application Specifications\n{specs}"),
            ]
        )
        design = result.content
        print(
            f"[DESIGN] [INFO] Functional design extracted ({len(design)} chars)"
        )
    except Exception as e:
        print(
            f"[DESIGN] [MEDIUM] Structured design failed ({type(e).__name__}), trying raw …"
        )
        try:
            response = llm.invoke(
                [
                    SystemMessage(content=FUNCTIONAL_DESIGN_SYSTEM_PROMPT),
                    HumanMessage(
                        content=f"## Application Specifications\n{specs}"
                    ),
                ]
            )
            design = response.content
            print(
                f"[DESIGN] [INFO] Functional design extracted via raw ({len(design)} chars)"
            )
        except Exception as e2:
            _log(
                "high",
                "DESIGN",
                f"Functional design extraction failed ({type(e2).__name__}). Proceeding without it.",
            )
            return ""
        else:
            return design
    else:
        return design


# ---------------------------------------------------------------------------
# Phase 4: Self-Validation (AIDLC-inspired)
# ---------------------------------------------------------------------------


def _run_self_validation(
    llm: BaseChatModel, code_files: list[dict]
) -> list[dict]:
    """Check consistency across all generated files before QA.

    Validates import consistency, interface matching, route registration,
    test coverage, and dependency completeness.

    Args:
        llm: LangChain LLM instance.
        code_files: List of generated CodeFile dicts.

    Returns:
        List of issue dicts (empty if validation passed).
    """
    if not code_files:
        return []

    print("\n[VALIDATE] Running self-validation on generated files …")

    # Build a summary of all files for the validation prompt
    file_summary = []
    for f in code_files:
        content = f.get("content", "")
        # Truncate to keep prompt within limits
        if len(content) > 3000:
            content = content[:3000] + "\n# ... (truncated)"
        file_summary.append(
            f"### {f['path']}\n```{f.get('language', '')}\n{content}\n```"
        )

    all_files_text = "\n\n".join(file_summary)

    try:
        val_llm = llm.with_structured_output(ValidationResult)
        result: ValidationResult = val_llm.invoke(
            [
                SystemMessage(content=VALIDATE_SYSTEM_PROMPT),
                HumanMessage(content=f"## Generated Files\n\n{all_files_text}"),
            ]
        )
        if result.passed:
            print("[VALIDATE] [INFO] ✓ All consistency checks passed")
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


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


def developer_node(state: AgentState) -> dict:
    """LangGraph node: run the Developer agent with AIDLC-inspired workflow.

    Orchestrates a four-phase process:
      1. Functional Design — extract domain entities, business rules, NFRs.
      2. Plan — determine which files to generate in dependency order.
      3. Generate — call the LLM once per file with cross-file context.
      4. Validate — check consistency across all generated files.

    On retry iterations, only files flagged by QA are regenerated; intact files
    are carried over from the previous state unchanged.

    Args:
        state: Current LangGraph agent state containing specs, prior code files,
               QA feedback, QA issues, and iteration count.

    Returns:
        A dict with updated ``functional_design``, ``code_files``, and
        ``reasoning_logs`` keys.
    """
    print("\n" + "=" * 60)
    print("👨‍💻 DEVELOPER AGENT — AIDLC-Inspired Workflow")
    print("=" * 60)

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.2, max_tokens=8000)

    specs = state.get("specs", "")
    qa_feedback = state.get("qa_feedback", "")
    qa_issues = state.get("qa_issues", [])
    iteration = state.get("iteration", 0)

    # ── Phase 1: Functional Design ───────────────────────────────────────────

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

    # ── Phase 2: File Planning ───────────────────────────────────────────────

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

    # ── Step 2a: LLM file plan (when not pre-determined) ─────────────────────

    if file_plan is None:
        print("[PLAN] Asking LLM for file plan …")
        try:
            plan_llm = llm.with_structured_output(FilePlan)
            plan_msg = f"## Specifications\n{specs}"
            if functional_design:
                plan_msg += f"\n\n## Functional Design\n{functional_design}"
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
            _log(
                "medium",
                "PLAN",
                f"Structured plan failed ({type(e).__name__}), using mandatory list.",
            )
            file_plan = list(MANDATORY_FILES)

        # Guarantee mandatory files are always included.
        for mf in MANDATORY_FILES:
            if not any(mf in fp for fp in file_plan):
                file_plan.append(mf)

    # Sort by architectural layer (AIDLC dependency ordering).
    file_plan = _sort_by_layer(file_plan)
    print(
        f"[PLAN] File plan ({len(file_plan)} files, dependency-ordered): {file_plan}"
    )

    # ── Phase 3: Generate each file individually ─────────────────────────────

    code_files: list[dict] = []
    generated: dict[str, dict] = {}  # For cross-file context lookups
    existing_files: dict[str, dict] = {
        f["path"]: f for f in state.get("code_files", [])
    }

    # Pre-populate generated context with existing files (for retry iterations)
    if iteration > 0 and existing_files:
        generated.update(existing_files)

    for i, file_path in enumerate(file_plan, 1):
        file_path = _sanitize_path(file_path)
        language = _detect_language(file_path)

        print(f"\n[ACT]  Generating file {i}/{len(file_plan)}: {file_path} …")

        file_hint = _get_file_hint(file_path)
        import_hint = _build_import_hint(file_path)

        # AIDLC-inspired: Cross-file context from already-generated dependencies
        dep_context = _resolve_dependencies(file_path, generated)
        if dep_context:
            dep_count = dep_context.count("###")
            print(
                f"[ACT]    ↳ Injecting {dep_count} dependency file(s) as context"
            )

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

        # Build the user message with all context layers
        user_msg = f"## Application Specifications\n{specs}\n\n"
        if functional_design:
            user_msg += f"## Functional Design\n{functional_design}\n\n"
        user_msg += (
            f"## File to generate\nPath: `{file_path}`\n\n"
            f"## Instructions\n{file_hint}"
            f"{import_hint}"
            f"{dep_context}"
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
            _log(
                "medium",
                "ACT",
                f"Structured failed ({type(e).__name__}), trying raw …",
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
                _log(
                    "high",
                    "ACT",
                    f"Raw also failed ({type(e2).__name__}), skipping file.",
                )
                continue

        content = _strip_markdown_fences(content)

        if content.strip():
            file_dict = {
                "path": file_path,
                "content": content,
                "language": language,
            }
            code_files.append(file_dict)
            generated[file_path] = file_dict  # Available for downstream files
            print(f"[ACT]    ✓ {len(content)} chars")
        else:
            _log("high", "ACT", f"Empty content for {file_path}, skipping")

    # On retry merges: preserve old files that were not regenerated.
    if iteration > 0 and existing_files:
        regenerated_paths = {f["path"] for f in code_files}
        for path, old_file in existing_files.items():
            if path not in regenerated_paths:
                code_files.append(old_file)
                print(f"[ACT]  Kept existing file: {path}")

    # ── Phase 4: Self-Validation (AIDLC-inspired) ───────────────────────────

    validation_issues = _run_self_validation(llm, code_files)

    if validation_issues:
        # Attempt one auto-fix pass for files with critical/major issues
        critical_files = {
            iss["file"]
            for iss in validation_issues
            if iss["severity"] in ("critical", "major")
        }

        if critical_files:
            print(
                f"\n[VALIDATE] Auto-fixing {len(critical_files)} file(s) with critical/major issues …"
            )
            fix_text = "\n".join(
                f"- [{iss['severity'].upper()}] {iss['file']}: {iss['description']}"
                for iss in validation_issues
            )

            for file_path in critical_files:
                # Find the file in code_files
                file_idx = next(
                    (
                        i
                        for i, f in enumerate(code_files)
                        if f["path"] == file_path
                    ),
                    None,
                )
                if file_idx is None:
                    continue

                print(f"[VALIDATE] Regenerating: {file_path} …")
                file_hint = _get_file_hint(file_path)
                import_hint = _build_import_hint(file_path)
                dep_context = _resolve_dependencies(file_path, generated)

                fix_msg = f"## Application Specifications\n{specs}\n\n"
                if functional_design:
                    fix_msg += f"## Functional Design\n{functional_design}\n\n"
                fix_msg += (
                    f"## File to generate\nPath: `{file_path}`\n\n"
                    f"## Instructions\n{file_hint}"
                    f"{import_hint}"
                    f"{dep_context}"
                    f"\n## Consistency issues to fix in THIS file:\n{fix_text}"
                )

                try:
                    content_llm = llm.with_structured_output(SingleFileContent)
                    result_file = content_llm.invoke(
                        [
                            SystemMessage(content=GENERATE_SYSTEM_PROMPT),
                            HumanMessage(content=fix_msg),
                        ]
                    )
                    content = _strip_markdown_fences(result_file.content)
                    if content.strip():
                        code_files[file_idx] = {
                            "path": file_path,
                            "content": content,
                            "language": code_files[file_idx]["language"],
                        }
                        generated[file_path] = code_files[file_idx]
                        print(
                            f"[VALIDATE] ✓ Fixed {file_path} ({len(content)} chars)"
                        )
                except Exception as e:
                    _log(
                        "medium",
                        "VALIDATE",
                        f"Auto-fix failed for {file_path} ({type(e).__name__})",
                    )

    # ── Reason phase ─────────────────────────────────────────────────────────

    file_list = (
        ", ".join(f["path"] for f in code_files) if code_files else "(none)"
    )
    reason_trace = f"Generated {len(code_files)} files: {file_list}"
    if validation_issues:
        reason_trace += f" | Self-validation found {len(validation_issues)} issues (auto-fix attempted)"
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
