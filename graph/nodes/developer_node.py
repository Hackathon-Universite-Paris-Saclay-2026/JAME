"""Developer node — Generates application code and tests.

Implements an AIDLC-inspired four-phase workflow:

  Phase 1  —  Functional Design: extract domain entities, business rules,
              and component dependencies from the specs (technology-agnostic).
  Phase 2  —  File Planning: determine which files to generate, in strict
              dependency order (driven by the Architect's specs).
  Phase 3  —  Code Generation: generate each file individually, passing
              already-generated dependency files as cross-reference context.
  Phase 4  —  Self-Validation: check consistency across all generated files
              before handing off to QA.

The developer does NOT hardcode any tech stack — it derives language, framework,
and project structure entirely from the Architect's specifications.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from cancel_token import raise_if_cancelled
from graph.prompts.developer_prompts import (
    FUNCTIONAL_DESIGN_SYSTEM_PROMPT,
    GENERATE_SYSTEM_PROMPT,
    LIGHTWEIGHT_GENERATE_SYSTEM_PROMPT,
    LIGHTWEIGHT_PLAN_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
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
from utils.node import strip_thinking


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIGHTWEIGHT_SCOPES = ("function", "feature")

_EXT_TO_LANG = {
    # Python
    ".py": "python",
    # JavaScript / TypeScript
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    # Web
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".vue": "vue",
    ".svelte": "svelte",
    # Data / config
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".ini": "ini",
    ".env": "text",
    ".properties": "properties",
    # Docs
    ".txt": "text",
    ".md": "markdown",
    ".rst": "restructuredtext",
    # Go
    ".go": "go",
    # Rust
    ".rs": "rust",
    # Java / Kotlin
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".gradle": "groovy",
    # C / C++
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    # C#
    ".cs": "csharp",
    # Ruby
    ".rb": "ruby",
    # PHP
    ".php": "php",
    # Swift
    ".swift": "swift",
    # Shell
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    # SQL
    ".sql": "sql",
    # Docker
    ".dockerfile": "dockerfile",
    # Elixir / Erlang
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    # Scala
    ".scala": "scala",
    # Lua
    ".lua": "lua",
    # R
    ".r": "r",
    ".R": "r",
}

# ---------------------------------------------------------------------------
# Per-file token budgets (tech-agnostic heuristic)
# ---------------------------------------------------------------------------
# Files whose role is "config / manifest" are short; files that contain real
# business logic or test suites need more room.  We match by suffix or by
# well-known file name so this works regardless of directory layout.

_NAME_MAX_TOKENS: dict[str, int] = {
    # Dependency manifests / config
    "requirements.txt": 512,
    "package.json": 512,
    "go.mod": 512,
    "Cargo.toml": 1024,
    "pom.xml": 1024,
    "build.gradle": 1024,
    "pyproject.toml": 1024,
    ".gitignore": 256,
    ".env.example": 256,
}

_SUFFIX_MAX_TOKENS: dict[str, int] = {
    ".json": 1024,
    ".yaml": 1024,
    ".yml": 1024,
    ".toml": 1024,
    ".xml": 2048,
    ".md": 2048,
    ".txt": 512,
    ".sql": 4000,
}

_DEFAULT_MAX_TOKENS = 6000


def _get_max_tokens(file_path: str) -> int:
    """Return the token budget for a given source file."""
    name = PurePosixPath(file_path).name
    if name in _NAME_MAX_TOKENS:
        return _NAME_MAX_TOKENS[name]
    suffix = PurePosixPath(file_path).suffix
    if suffix in _SUFFIX_MAX_TOKENS:
        return _SUFFIX_MAX_TOKENS[suffix]
    # Test files tend to be longer
    if name.startswith("test_") or name.endswith("_test.py") or "/test" in file_path:
        return 8000
    return _DEFAULT_MAX_TOKENS


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
    suffix = PurePosixPath(path).suffix
    if not suffix:
        # Handle extensionless files like Dockerfile, Makefile, etc.
        name = PurePosixPath(path).name.lower()
        if name == "dockerfile":
            return "dockerfile"
        if name == "makefile":
            return "makefile"
        return "text"
    return _EXT_TO_LANG.get(suffix, "text")


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


def _resolve_generated_context(generated: dict[str, dict]) -> str:
    """Build a cross-reference section from ALL already-generated files.

    Instead of using a hardcoded dependency map, we pass all previously
    generated files so the LLM can match interfaces exactly — regardless
    of the tech stack.
    """
    if not generated:
        return ""
    sections: list[str] = []
    for path, file_dict in generated.items():
        content = file_dict.get("content", "")
        if not content:
            continue
        if len(content) > 4000:
            content = content[:4000] + "\n# ... (truncated for brevity)"
        sections.append(f"### {path}\n```\n{content}\n```")
    if not sections:
        return ""
    return (
        "\n## Already-generated files (use EXACT interfaces)\n"
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
            _, content = strip_thinking(result.content)
            return content
    try:
        _, content = strip_thinking(llm.invoke(messages).content)
    except Exception as e:
        _log("high", phase, f"LLM call failed ({type(e).__name__})")
        return None
    else:
        return content


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
    dep_context = _resolve_generated_context(generated)
    if dep_context:
        dep_count = dep_context.count("###")
        print(
            f"[ACT]    \u21b3 Injecting {dep_count} "
            "already-generated file(s) as context"
        )

    msg = f"## Application Specifications\n{specs}\n\n"
    if functional_design:
        msg += f"## Functional Design\n{functional_design}\n\n"
    msg += (
        f"## File to generate\nPath: `{file_path}`\n\n"
        f"## Instructions\n"
        f"Generate the complete, production-quality content for: {file_path}\n"
        f"Follow the tech stack and architecture defined in the specifications above."
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
    project_dir: Path | None = None,
) -> dict | None:
    """Generate a single file. Returns its dict or ``None`` on failure."""
    user_msg = _build_file_prompt(
        file_path, specs, functional_design, generated, file_issues
    )
    content = _invoke_llm(
        llm.bind(max_tokens=_get_max_tokens(file_path)),
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

    if project_dir is not None:
        dest = project_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

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
    """Determine which files to generate. Returns ``(file_plan, plan_trace)``.

    The file plan is derived entirely from the Architect's specs — no
    hardcoded mandatory files.
    """
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
        if has_general or not flagged:
            # GENERAL issues or no specific files → re-plan everything
            file_plan = None
        else:
            file_plan = list(flagged)
        if file_plan is not None:
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
                "high",
                "PLAN",
                f"Structured plan failed ({type(e).__name__}). "
                "Cannot proceed without a file plan.",
            )
            file_plan = []

    # Guarantee package.json for JavaScript/TypeScript projects.
    _js_exts = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"})
    has_js = any(PurePosixPath(fp).suffix in _js_exts for fp in file_plan)
    has_pkg = any(
        fp == "package.json" or fp.endswith("/package.json") for fp in file_plan
    )
    if has_js and not has_pkg:
        file_plan.append("package.json")
        print("[PLAN] Injected missing package.json for JS/TS project.")

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
    project_dir: Path | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Generate all planned files. Returns ``(code_files, generated_map)``."""
    code_files: list[dict] = []
    generated: dict[str, dict] = {}

    # Pre-populate context from prior iteration so dependencies are available.
    if iteration > 0 and existing_files:
        generated.update(existing_files)

    for i, file_path in enumerate(file_plan, 1):
        raise_if_cancelled()
        file_path = _sanitize_path(file_path)
        print(
            f"\n[ACT]  Generating file {i}/{len(file_plan)}: {file_path} \u2026"
        )

        file_issues = _collect_file_issues(
            file_path, qa_issues, qa_feedback, iteration
        )
        result = _generate_file(
            llm,
            file_path,
            specs,
            functional_design,
            generated,
            file_issues,
            project_dir,
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
    project_dir: Path | None = None,
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
            project_dir=project_dir,
        )
        if result:
            code_files[file_idx] = result
            generated[file_path] = result


# ---------------------------------------------------------------------------
# Lightweight path (function / feature scope)
# ---------------------------------------------------------------------------


def _run_lightweight(
    llm: BaseChatModel,
    scope: str,
    specs: str,
    project_dir: Path | None = None,
) -> list[dict]:
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
        raise_if_cancelled()
        file_path = _sanitize_path(file_path)
        print(
            f"\n[ACT]  Generating file {i}/{len(file_plan)}: {file_path} \u2026"
        )

        user_msg = (
            f"## Specifications\n{specs}\n\n"
            f"## File to generate\nPath: `{file_path}`"
            f"{_resolve_generated_context(generated)}"
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
            if project_dir is not None:
                dest = project_dir / file_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
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

    The tech stack is derived entirely from the Architect's specs — no
    framework is hardcoded.
    """
    raise_if_cancelled()
    print("\n" + "=" * 60)
    print(
        "\U0001f468\u200d\U0001f4bb DEVELOPER AGENT \u2014 AIDLC-Inspired Workflow"
    )
    print("=" * 60)

    _emit = state.get("emit_callback")

    def _log_emit(entry: dict) -> None:
        if _emit:
            _emit(entry)

    _log_emit(
        {
            "agent": "developer",
            "phase": "plan",
            "content": "Developer agent starting\u2026",
        }
    )
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
        lw_run_output_dir = state.get("run_output_dir", "")
        lw_project_dir: Path | None = None
        if lw_run_output_dir:
            lw_project_dir = (
                Path(lw_run_output_dir) / "output" / "project"
            ).resolve()
            lw_project_dir.mkdir(parents=True, exist_ok=True)
        _log_emit(
            {
                "agent": "developer",
                "phase": "plan",
                "content": f"Lightweight scope ({scope}) \u2014 minimal file set.",
            }
        )
        code_files = _run_lightweight(llm, scope, specs, lw_project_dir)
        file_list = (
            ", ".join(f["path"] for f in code_files) if code_files else "(none)"
        )
        reason = f"Lightweight ({scope}): generated {len(code_files)} file(s): {file_list}"
        _log_emit(
            {
                "agent": "developer",
                "phase": "act",
                "content": f"Generated {len(code_files)} file(s).",
            }
        )
        print(f"\n[REASON] {reason}\n")
        _log_emit({"agent": "developer", "phase": "reason", "content": reason})
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
    raise_if_cancelled()
    functional_design = state.get("functional_design", "")
    if iteration == 0:
        _log_emit(
            {
                "agent": "developer",
                "phase": "design",
                "content": "Extracting functional design\u2026",
            }
        )
        functional_design = _run_functional_design(llm, specs)
        design_trace = (
            f"Extracted functional design ({len(functional_design)} chars): "
            "domain entities, business rules, API endpoints, NFR considerations."
        )
    else:
        design_trace = "Reusing functional design from previous iteration."
        print(f"\n[DESIGN] {design_trace}")
    _log_emit({"agent": "developer", "phase": "design", "content": design_trace})

    # ── Phase 2: File Planning ───────────────────────────────────
    raise_if_cancelled()
    _log_emit(
        {
            "agent": "developer",
            "phase": "plan",
            "content": "Planning files to generate\u2026",
        }
    )
    file_plan, plan_trace = _run_file_planning(
        llm,
        specs,
        functional_design,
        qa_issues,
        qa_feedback,
        iteration,
    )
    _log_emit({"agent": "developer", "phase": "plan", "content": plan_trace})

    # ── Phase 3: Code Generation ─────────────────────────────────
    run_output_dir = state.get("run_output_dir", "")
    project_dir: Path | None = None
    if run_output_dir:
        project_dir = (Path(run_output_dir) / "output" / "project").resolve()
        project_dir.mkdir(parents=True, exist_ok=True)

    _log_emit(
        {
            "agent": "developer",
            "phase": "act",
            "content": "Generating code files (dependency-ordered)\u2026",
        }
    )
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
        project_dir,
    )
    _log_emit(
        {
            "agent": "developer",
            "phase": "act",
            "content": f"Chunked generation: {len(code_files)} files produced.",
        }
    )

    # ── Phase 4: Self-Validation ─────────────────────────────────
    _log_emit(
        {
            "agent": "developer",
            "phase": "validate",
            "content": "Running self-validation\u2026",
        }
    )
    validation_issues = _run_self_validation(llm, code_files)
    if validation_issues:
        _run_auto_fix(
            llm,
            code_files,
            generated,
            validation_issues,
            specs,
            functional_design,
            project_dir,
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
    _log_emit({"agent": "developer", "phase": "reason", "content": reason_trace})

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
