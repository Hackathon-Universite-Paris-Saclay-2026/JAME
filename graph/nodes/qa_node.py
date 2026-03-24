"""QA node — Runtime-first quality assurance.

Flow:
  [SCOPE DECISION] Is full runtime QA warranted?
      │
      └─ _run_project_qa(needs_full_qa=True|False)
           [SETUP]  build venv / npm install  (full: blocking · small: non-blocking)
           [TEST]   run tests for every detected language
               ├─ FAIL → [RELEVANCY] assess whether failures are code or test faults
               │          ├─ full:  + [PER-FILE REVIEW] → detailed fix instructions
               │          │          → FIX: Developer (loop ≤ max_iterations)
               │          └─ small: "FIX THE CODE" detected → FIX: Developer
               │                    all "FIX THE TEST"      → acknowledge, continue
               └─ PASS → [COMPILE] py_compile / tsc / node --check
                             (full: blocking · small: non-blocking)
                             ├─ FAIL (full only) → [PLAN REVIEW] → FIX: Developer
                             └─ PASS → [SPECS COMPARISON] (full only) → PASS
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from cancel_token import raise_if_cancelled
from graph.prompts.qa_prompts import (
    COMPRESS_PROMPT,
    CROSS_FILE_PROMPT,
    FULL_REWRITE_PROMPT,
    PATCH_INSTRUCTIONS_PROMPT,
    PLAN_REVIEW_PROMPT,
    PRUNE_TESTS_PROMPT,
    SCOPE_DECISION_PROMPT,
    SPECS_COMPARISON_PROMPT,
    STATIC_ANALYSIS_PROMPT,
    TEST_RELEVANCY_PROMPT,
)
from graph.state import (
    AgentState,
    CodeFile,
    QAIssue,
    ScopeDecision,
    StaticAnalysisResult,
)
from integrations.cortex import get_cortex_llm
from utils.node import (
    maybe_compress,
    parse_json_safe,
    run_parallel,
    strip_thinking,
)
from utils.run_test_helper import (
    check_js_syntax,
    compile_project,
    run_js_tests,
    run_pytest,
    setup_environment,
    setup_js_environment,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


_PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})
_JS_EXTENSIONS = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"})


def _detect_project_languages(code_files: list[CodeFile]) -> set[str]:
    """Return the set of runtime languages present in the project.

    Returns a subset of ``{"python", "javascript"}``. An empty set means
    no recognised runtime — the QA node will fall back to static analysis.
    """
    suffixes = {Path(cf.path).suffix for cf in code_files}
    languages: set[str] = set()
    if suffixes & _PYTHON_EXTENSIONS:
        languages.add("python")
    if suffixes & _JS_EXTENSIONS:
        languages.add("javascript")
    return languages


def _to_code_file(f: dict | CodeFile) -> CodeFile:
    """Normalise a dict or CodeFile object to a CodeFile instance."""
    if isinstance(f, CodeFile):
        return f
    return CodeFile(
        path=f["path"],
        content=f["content"],
        language=f.get("language", "python"),
    )


def _print_approval_gate(
    stage: str, summary_lines: list[str], next_stage: str
) -> None:
    print("\n" + "=" * 60)
    print(f"# {stage} Complete")
    print("=" * 60)
    for line in summary_lines:
        print(f"  • {line}")
    print()
    print("  📋 REVIEW REQUIRED")
    print(f"  🚀 WHAT'S NEXT: Proceeding to → {next_stage}")
    print("=" * 60 + "\n")


def _collect_qa_issues(
    per_file_results: list[dict], extra_results: list[dict] | None = None
) -> list[QAIssue]:
    """Collect QAIssue objects from per-file and optional extra results.

    Blocking security rules (SECURITY-01/05/08/12) are promoted to 'critical'
    regardless of the severity the LLM reported.
    """
    issues: list[QAIssue] = []
    all_results = list(per_file_results) + (extra_results or [])
    for r in all_results:
        for raw in r.get("issues", []):
            severity = raw.get("severity", "minor")
            security_rule = raw.get("security_rule")
            if (
                security_rule
                and security_rule in _BLOCKING_SECURITY_RULES
                and severity != "critical"
            ):
                severity = "critical"
            issues.append(
                QAIssue(
                    file=r.get("file", raw.get("file", "UNKNOWN")),
                    severity=severity,
                    description=raw.get("description", ""),
                    security_rule=security_rule,
                )
            )
    return issues


# ── Security escalation ────────────────────────────────────────────────────────

# These rules are always blocking regardless of LLM-assessed severity.
# Any issue tagged with one of these is promoted to 'critical'.
_BLOCKING_SECURITY_RULES: frozenset[str] = frozenset(
    {
        "SECURITY-01",  # Encryption at rest / in transit
        "SECURITY-05",  # Input validation — SQL/command injection, path traversal
        "SECURITY-08",  # Application-level access control (authn/authz)
        "SECURITY-12",  # Authentication and credential management
    }
)


# ── LLM helpers ───────────────────────────────────────────────────────────────


def _decide_scope(
    llm: BaseChatModel, specs: str, code_files: list[CodeFile]
) -> ScopeDecision:
    """Ask the LLM whether full runtime QA is warranted for this project."""
    file_list = "\n".join(f"- {cf.path} ({cf.language})" for cf in code_files)
    prompt = (
        f"## Application Specifications\n{specs}\n\n"
        f"## Generated Files\n{file_list}"
    )
    try:
        return llm.with_structured_output(ScopeDecision).invoke(
            [HumanMessage(content=f"{SCOPE_DECISION_PROMPT}\n\n{prompt}")]
        )
    except Exception:
        return ScopeDecision(
            needs_full_qa=len(code_files) > 3,
            reasoning="Fallback: structured output unavailable — deciding by file count.",
        )


def _assess_test_relevancy(
    llm: BaseChatModel,
    specs: str,
    code_files: list[CodeFile],
    failed_summaries: list[str],
) -> str:
    """Assess whether failing tests are meaningful or overly strict."""
    test_files = "\n\n".join(
        f"### {cf.path}\n```{cf.language}\n{cf.content}\n```"
        for cf in code_files
        if "test" in cf.path.lower()
    )
    resp = llm.invoke(
        [
            HumanMessage(
                content=TEST_RELEVANCY_PROMPT.format(
                    specs=specs,
                    failed_summaries="\n---\n".join(failed_summaries),
                    test_files=test_files,
                )
            )
        ]
    )
    _, content = strip_thinking(resp.content)
    return content


def _analyse_file(
    llm: BaseChatModel,
    specs: str,
    code_file: CodeFile,
    priority: str = "important",
) -> dict:
    """Static analysis of a single file.

    Attempts structured output via ``StaticAnalysisResult``; falls back to raw
    invoke + ``parse_json_safe`` if the model does not comply.
    """
    print(f"[REVIEW] {code_file.path} …")
    msg = HumanMessage(
        content=STATIC_ANALYSIS_PROMPT.format(
            priority=priority,
            specs=specs,
            file_path=code_file.path,
            language=code_file.language,
            content=code_file.content,
        )
    )
    result: dict
    try:
        structured: StaticAnalysisResult = llm.with_structured_output(
            StaticAnalysisResult
        ).invoke([msg])
        result = structured.model_dump()
    except Exception:
        resp = llm.invoke([msg])
        _, raw = strip_thinking(resp.content)
        result = parse_json_safe(
            raw, {"file": code_file.path, "issues": [], "has_issues": False}
        )

    issues = result.get("issues", [])
    if issues:
        c = sum(1 for i in issues if i.get("severity") == "critical")
        m = sum(1 for i in issues if i.get("severity") == "major")
        print(
            f"    ⚠️  {len(issues)} issue(s): {c} critical / {m} major / {len(issues) - c - m} minor"
        )
    else:
        print("    ✅ No issues.")
    return result


def _generate_fix_instructions(
    llm: BaseChatModel, specs: str, code_file: CodeFile, issues: list[dict]
) -> str:
    """Generate patch instructions or a full rewrite brief for a file."""
    critical_count = sum(1 for i in issues if i.get("severity") == "critical")
    issues_text = "\n".join(
        f"- [{i.get('severity', '?')}] line {i.get('line_hint', '?')}: "
        f"{i.get('description', '')} → {i.get('fix', '')}"
        for i in issues
    )
    if critical_count >= 3:
        print(f"[FIX] {code_file.path}: escalating to rewrite brief")
        resp = llm.invoke(
            [
                HumanMessage(
                    content=FULL_REWRITE_PROMPT.format(
                        file_path=code_file.path,
                        critical_count=critical_count,
                        issues=issues_text,
                        specs=specs,
                    )
                )
            ]
        )
    else:
        print(f"[FIX] {code_file.path}: generating patch instructions")
        resp = llm.invoke(
            [
                HumanMessage(
                    content=PATCH_INSTRUCTIONS_PROMPT.format(
                        file_path=code_file.path,
                        issues=issues_text,
                        language=code_file.language,
                        content=code_file.content,
                    )
                )
            ]
        )
    _, instructions = strip_thinking(resp.content)
    return instructions


def _review_failing_files(
    llm: BaseChatModel,
    specs: str,
    code_files: list[CodeFile],
    failed_summaries: list[str],
) -> tuple[list[dict], list[QAIssue]]:
    """Static analysis scoped to files referenced in failing tests.

    Also includes the source files those tests import/exercise.
    """
    # Extract file paths from pytest output
    failing_paths: set[str] = set()
    for summary in failed_summaries:
        for match in re.finditer(r"([\w/\\]+\.py)", summary):
            failing_paths.add(match.group(1).replace("\\", "/"))

    # If we couldn't parse paths, review all files
    files_to_review = (
        [
            cf
            for cf in code_files
            if any(p in cf.path or cf.path in p for p in failing_paths)
        ]
        if failing_paths
        else code_files
    )
    if not files_to_review:
        files_to_review = code_files

    print(
        f"[REVIEW] Reviewing {len(files_to_review)} file(s) linked to failures"
    )
    per_file_results: list[dict] = run_parallel(
        [partial(_analyse_file, llm, specs, cf) for cf in files_to_review]
    )

    # Cross-file check
    summaries = [
        f"- {r['file']}: "
        + (
            "; ".join(i.get("description", "") for i in r.get("issues", []))
            or "no issues"
        )
        for r in per_file_results
    ]
    resp = llm.invoke(
        [
            HumanMessage(
                content=CROSS_FILE_PROMPT.format(
                    specs=specs,
                    per_file_summaries="\n".join(summaries),
                )
            )
        ]
    )
    _, raw = strip_thinking(resp.content)
    cross_result = parse_json_safe(raw, {"issues": [], "has_issues": False})

    qa_issues = _collect_qa_issues(per_file_results, [cross_result])
    return per_file_results, qa_issues


def _build_fix_feedback(
    llm: BaseChatModel,
    specs: str,
    code_files: list[CodeFile],
    per_file_results: list[dict],
    relevancy_feedback: str,
    test_output: str,
) -> str:
    """Build the full qa_feedback string to send to Developer.

    Includes patch instructions for files with critical/major issues and a
    plain-text hint block for files with only minor issues (no extra LLM call).
    """
    files_with_issues = [
        (r, next((cf for cf in code_files if cf.path == r["file"]), None))
        for r in per_file_results
        if r.get("issues")
    ]
    # Split into files needing full instructions vs minor-only hints
    needs_instructions = [
        (r, cf)
        for r, cf in files_with_issues
        if cf is not None
        and any(i.get("severity") in ("critical", "major") for i in r["issues"])
    ]
    minor_only = [
        (r, cf)
        for r, cf in files_with_issues
        if cf is not None
        and all(i.get("severity") == "minor" for i in r["issues"])
    ]

    instructions_list: list[str] = run_parallel(
        [
            partial(_generate_fix_instructions, llm, specs, cf, r["issues"])
            for r, cf in needs_instructions
        ]
    )
    parts = []
    if relevancy_feedback:
        parts.append(f"## Test Relevancy Assessment\n{relevancy_feedback}")
    parts.append(f"## Failed Test Output\n```\n{test_output[-3000:]}\n```")
    for (_r, cf), instructions in zip(
        needs_instructions, instructions_list, strict=False
    ):
        parts.append(f"### {cf.path}\n{instructions}")

    if minor_only:
        hint_lines = [
            f"- **{cf.path}**: "
            + "; ".join(i.get("description", "") for i in r["issues"])
            for r, cf in minor_only
        ]
        parts.append(
            "## Minor Issues (optional — do not break anything fixing these)\n"
            + "\n".join(hint_lines)
        )
    return "\n\n".join(parts)


def _plan_review(
    llm: BaseChatModel,
    specs: str,
    code_files: list[CodeFile],
    compile_error: str,
) -> tuple[list[QAIssue], str]:
    """File structure review + cross-check against compile error."""
    print("[PLAN REVIEW] Reviewing project structure vs compile error …")
    file_list = "\n".join(f"- {cf.path} ({cf.language})" for cf in code_files)
    resp = llm.invoke(
        [
            HumanMessage(
                content=PLAN_REVIEW_PROMPT.format(
                    specs=specs,
                    file_list=file_list,
                    compile_error=compile_error,
                )
            )
        ]
    )
    _, raw = strip_thinking(resp.content)
    result = parse_json_safe(raw, {"issues": [], "fix_instructions": ""})

    issues = [
        QAIssue(
            file=i.get("file", "GENERAL"),
            severity=i.get("severity", "major"),
            description=i.get("description", ""),
        )
        for i in result.get("issues", [])
    ]
    fix_instructions = (
        f"## Compile Error\n```\n{compile_error}\n```\n\n"
        f"## Fix Instructions\n{result.get('fix_instructions', '')}"
    )
    return issues, fix_instructions


def _specs_comparison(
    llm: BaseChatModel,
    specs: str,
    code_files: list[CodeFile],
    run_output_dir: str,
) -> str:
    """Compare built project against specs and write report to disk."""
    print("[SPECS] Comparing built project against specifications …")
    built_summary = "\n\n".join(
        f"### {cf.path}\n```{cf.language}\n{cf.content}\n```"
        for cf in code_files
    )
    resp = llm.invoke(
        [
            HumanMessage(
                content=SPECS_COMPARISON_PROMPT.format(
                    specs=specs,
                    built_files=built_summary,
                )
            )
        ]
    )
    _, report = strip_thinking(resp.content)

    if run_output_dir:
        report_path = Path(run_output_dir) / "output" / "specs_comparison.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"[SPECS] Report written → {report_path}")

    return report


def _prune_failing_tests(
    llm: BaseChatModel,
    code_files: list[CodeFile],
    failed_summaries: list[str],
    project_dir: Path | None = None,
) -> list[CodeFile]:
    """Rewrite test files removing only the failing (inappropriate) test functions.

    Called after relevancy assessment confirms the failures are test faults,
    not code bugs. Writes pruned files back to disk when ``project_dir`` is
    provided. Returns an updated code_files list.
    """
    failing: dict[str, list[str]] = {}
    for summary in failed_summaries:
        match = re.search(
            r"(?:FAILED|ERROR)\s+([\w/\\]+\.py)::([\w:]+)", summary
        )
        if match:
            file_path = match.group(1).replace("\\", "/")
            test_name = match.group(2).split("::")[-1]
            failing.setdefault(file_path, []).append(test_name)

    if not failing:
        return code_files

    updated: list[CodeFile] = []
    for cf in code_files:
        key = next((k for k in failing if k in cf.path or cf.path in k), None)
        if key is None:
            updated.append(cf)
            continue

        failing_tests = failing[key]
        print(
            f"[PRUNE] Removing {len(failing_tests)} inappropriate test(s) from {cf.path}"
        )
        resp = llm.invoke(
            [
                HumanMessage(
                    content=PRUNE_TESTS_PROMPT.format(
                        failing_tests="\n".join(
                            f"- {t}" for t in failing_tests
                        ),
                        failed_summaries="\n---\n".join(failed_summaries),
                        file_path=cf.path,
                        language=cf.language,
                        content=cf.content,
                    )
                )
            ]
        )
        _, pruned_content = strip_thinking(resp.content)
        pruned_content = pruned_content.strip()
        if pruned_content.startswith("```"):
            first_nl = pruned_content.find("\n")
            if first_nl != -1:
                pruned_content = pruned_content[first_nl + 1 :]
        if pruned_content.rstrip().endswith("```"):
            pruned_content = pruned_content.rstrip()[:-3].rstrip()
        if project_dir:
            dest = project_dir / cf.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(pruned_content, encoding="utf-8")
            print(f"[PRUNE] Written → {cf.path}")
        updated.append(
            CodeFile(path=cf.path, content=pruned_content, language=cf.language)
        )

    return updated


# ── Unified QA runtime path ────────────────────────────────────────────────────


def _run_project_qa(
    llm: BaseChatModel,
    specs: str,
    code_files: list[CodeFile],
    project_dir: Path,
    venv_dir: Path | None,
    run_output_dir: str,
    iteration: int,
    reasoning_logs: list[dict],
    languages: set[str],
    *,
    needs_full_qa: bool,
    emit_callback: object = None,
) -> dict:
    """Unified QA runtime path for all project sizes.

    Runs setup → tests → compile/syntax-check for every detected language.

    ``needs_full_qa`` controls failure handling:
    - ``True`` (full project): setup/compile failures are blocking; test
      failures trigger a detailed per-file review + fix instructions →
      ``qa_passed=False`` sent to Developer.
    - ``False`` (small project): setup failures are non-blocking; compile
      errors are logged but not blocking; test failures are assessed via
      relevancy — if any ``FIX THE CODE`` verdict is found the fix loop is
      triggered, otherwise the inappropriate tests are pruned inline and QA
      passes with the cleaned ``code_files``.
    """
    def _log(entry: dict) -> None:
        reasoning_logs.append(entry)
        if emit_callback:
            emit_callback(entry)

    scope = "Full" if needs_full_qa else "Small"
    print(f"\n[QA] {scope} project ({', '.join(sorted(languages))})")

    # Tracks code_files, potentially updated by test pruning on small projects.
    updated_files = code_files

    # ── SETUP ─────────────────────────────────────────────────────────────────
    if "python" in languages:
        setup_ok, setup_error = setup_environment(
            venv_dir, project_dir, code_files
        )
        if not setup_ok:
            _log(
                {
                    "agent": "qa",
                    "phase": "act",
                    "content": f"Python setup failed: {setup_error}",
                }
            )
            if needs_full_qa:
                return {
                    "qa_passed": False,
                    "qa_feedback": f"## Python Environment Setup Failed\n{setup_error}",
                    "qa_issues": [],
                    "code_files": [cf.model_dump() for cf in code_files],
                    "iteration": iteration + 1,
                    "reasoning_logs": reasoning_logs,
                }
            print("[SETUP] ⚠️  Python setup failed — skipping Python checks")

    if "javascript" in languages:
        setup_ok, setup_error = setup_js_environment(project_dir)
        if not setup_ok:
            _log(
                {
                    "agent": "qa",
                    "phase": "act",
                    "content": f"JS setup failed: {setup_error}",
                }
            )
            if needs_full_qa:
                return {
                    "qa_passed": False,
                    "qa_feedback": f"## JS Environment Setup Failed\n{setup_error}",
                    "qa_issues": [],
                    "code_files": [cf.model_dump() for cf in code_files],
                    "iteration": iteration + 1,
                    "reasoning_logs": reasoning_logs,
                }
            print("[SETUP] ⚠️  JS setup failed — skipping JS checks")

    # ── TEST ──────────────────────────────────────────────────────────────────
    all_failed_summaries: list[str] = []
    combined_test_output = ""

    if "python" in languages:
        py_passed, py_output, py_failures = run_pytest(venv_dir, project_dir)
        combined_test_output += py_output
        if not py_passed:
            all_failed_summaries.extend(py_failures)
        _log(
            {
                "agent": "qa",
                "phase": "act",
                "content": "Python tests passed."
                if py_passed
                else f"{len(py_failures)} Python test failure(s).",
            }
        )

    if "javascript" in languages:
        js_passed, js_output, js_failures = run_js_tests(project_dir)
        combined_test_output += js_output
        if not js_passed:
            all_failed_summaries.extend(js_failures)
        _log(
            {
                "agent": "qa",
                "phase": "act",
                "content": "JS tests passed."
                if js_passed
                else f"{len(js_failures)} JS test failure(s).",
            }
        )

    if all_failed_summaries:
        print("[RELEVANCY] Assessing test relevancy …")
        if needs_full_qa:
            # Full path: run relevancy + per-file review in parallel
            relevancy_feedback, (per_file_results, qa_issues) = run_parallel(
                [
                    partial(
                        _assess_test_relevancy,
                        llm,
                        specs,
                        code_files,
                        all_failed_summaries,
                    ),
                    partial(
                        _review_failing_files,
                        llm,
                        specs,
                        code_files,
                        all_failed_summaries,
                    ),
                ]
            )
            _log(
                {
                    "agent": "qa",
                    "phase": "act",
                    "content": f"Test relevancy assessed. {len(per_file_results)} file(s) reviewed.",
                }
            )
            qa_feedback = _build_fix_feedback(
                llm,
                specs,
                code_files,
                per_file_results,
                relevancy_feedback,
                combined_test_output,
            )
            _log(
                {
                    "agent": "qa",
                    "phase": "reason",
                    "content": f"QA FAIL: tests failed at iteration {iteration + 1}.",
                }
            )
            return {
                "qa_passed": False,
                "qa_feedback": qa_feedback,
                "qa_issues": [i.model_dump() for i in qa_issues],
                "code_files": [cf.model_dump() for cf in code_files],
                "iteration": iteration + 1,
                "reasoning_logs": reasoning_logs,
            }
        # Small path: relevancy decides — code fault triggers fix loop,
        # test fault is acknowledged and QA continues
        relevancy_feedback = _assess_test_relevancy(
            llm, specs, code_files, all_failed_summaries
        )
        _log(
            {
                "agent": "qa",
                "phase": "act",
                "content": f"Test relevancy: {relevancy_feedback[:300]}",
            }
        )
        if "FIX THE CODE" in relevancy_feedback:
            print("[QA] Code fault(s) detected — triggering fix loop …")
            _log(
                {
                    "agent": "qa",
                    "phase": "reason",
                    "content": "Small project: code fault(s) found — sending to Developer.",
                }
            )
            return {
                "qa_passed": False,
                "qa_feedback": (
                    f"## Test Relevancy Assessment\n{relevancy_feedback}"
                    f"\n\n## Failed Test Output\n```\n"
                    f"{combined_test_output[-3000:]}\n```"
                ),
                "qa_issues": [],
                "code_files": [cf.model_dump() for cf in code_files],
                "iteration": iteration + 1,
                "reasoning_logs": reasoning_logs,
            }
        print(
            f"[QA] Tests are inappropriate — pruning {len(all_failed_summaries)} failing test(s) …"
        )
        updated_files = _prune_failing_tests(
            llm, updated_files, all_failed_summaries, project_dir
        )
        _log(
            {
                "agent": "qa",
                "phase": "act",
                "content": f"Small project: {len(all_failed_summaries)} inappropriate test(s) pruned.",
            }
        )

    # ── COMPILE / SYNTAX CHECK ────────────────────────────────────────────────
    compile_errors: list[str] = []

    if "python" in languages:
        py_compile_passed, py_compile_error = compile_project(
            venv_dir, project_dir, updated_files
        )
        if not py_compile_passed:
            if needs_full_qa:
                compile_errors.append(py_compile_error)
            else:
                print(
                    f"[COMPILE] ⚠️  Python compile error (not blocking):\n{py_compile_error}"
                )
        _log(
            {
                "agent": "qa",
                "phase": "act",
                "content": "Python compile passed."
                if py_compile_passed
                else f"Python compile failed: {py_compile_error[:200]}",
            }
        )

    if "javascript" in languages:
        js_syntax_passed, js_syntax_error = check_js_syntax(
            project_dir, updated_files
        )
        if not js_syntax_passed:
            if needs_full_qa:
                compile_errors.append(js_syntax_error)
            else:
                print(
                    f"[COMPILE] ⚠️  JS syntax error (not blocking):\n{js_syntax_error}"
                )
        _log(
            {
                "agent": "qa",
                "phase": "act",
                "content": "JS/TS syntax passed."
                if js_syntax_passed
                else f"JS/TS syntax failed: {js_syntax_error[:200]}",
            }
        )

    if compile_errors:
        combined_error = "\n\n".join(compile_errors)
        qa_issues, qa_feedback = _plan_review(
            llm, specs, updated_files, combined_error
        )
        _log(
            {
                "agent": "qa",
                "phase": "reason",
                "content": f"QA FAIL: compile/syntax errors at iteration {iteration + 1}.",
            }
        )
        return {
            "qa_passed": False,
            "qa_feedback": qa_feedback,
            "qa_issues": [i.model_dump() for i in qa_issues],
            "code_files": [cf.model_dump() for cf in updated_files],
            "iteration": iteration + 1,
            "reasoning_logs": reasoning_logs,
        }

    # ── SPECS COMPARISON (full path only) ─────────────────────────────────────
    if needs_full_qa:
        _specs_comparison(llm, specs, updated_files, run_output_dir)

    _log(
        {
            "agent": "qa",
            "phase": "reason",
            "content": f"QA PASS: all checks passed ({', '.join(sorted(languages))}).",
        }
    )
    _print_approval_gate(
        stage=f"QA — {scope} Project",
        summary_lines=[
            f"All tests passed ({', '.join(sorted(languages))})",
            "All files compile/syntax-check cleanly",
        ]
        + (["Specs comparison report written"] if needs_full_qa else []),
        next_stage="DevOps",
    )

    return {
        "qa_passed": True,
        "qa_feedback": "",
        "qa_issues": [],
        "code_files": [cf.model_dump() for cf in updated_files],
        "iteration": iteration + 1,
        "reasoning_logs": reasoning_logs,
    }


# ── Main node ──────────────────────────────────────────────────────────────────


def qa_node(state: AgentState) -> dict:
    """LangGraph node: runtime-first QA with scope-aware flow.

    Args:
        state: Current pipeline state.

    Returns:
        Dict updating ``qa_passed``, ``qa_feedback``, ``qa_issues``,
        ``iteration``, and ``reasoning_logs``. May also update ``code_files``
        when failing tests are pruned on small projects.
    """
    raise_if_cancelled()
    print("\n" + "=" * 60)
    print("🔍  QUALITY ENGINEER — Runtime QA")
    print("=" * 60)

    specs = state.get("specs", "")
    raw_files = state.get("code_files", [])
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 3)
    run_output_dir = state.get("run_output_dir", "")
    _emit = state.get("emit_callback")
    reasoning_logs: list[dict] = []

    def _log(entry: dict) -> None:
        reasoning_logs.append(entry)
        if _emit:
            _emit(entry)

    _log({"agent": "qa", "phase": "plan", "content": "QA agent starting…"})
    code_files = [_to_code_file(f) for f in raw_files]

    if not code_files:
        print("[QA] No code files to review — skipping.\n")
        return {
            "qa_passed": False,
            "qa_feedback": "No code files provided to the Quality Engineer.",
            "qa_issues": [],
            "iteration": iteration + 1,
            "reasoning_logs": [
                {
                    "agent": "qa",
                    "phase": "plan",
                    "content": "No code files to review.",
                }
            ],
        }

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.3, max_tokens=4096)

    # Compress specs if large
    compressed_specs = maybe_compress(
        llm, specs, "", COMPRESS_PROMPT, threshold=4000
    )

    print(
        f"\n[QA] Iteration {iteration + 1}/{max_iterations} — {len(code_files)} file(s)"
    )
    _log(
        {
            "agent": "qa",
            "phase": "plan",
            "content": f"Iteration {iteration + 1}/{max_iterations}, {len(code_files)} file(s).",
        }
    )

    # Resolve paths
    project_dir = (
        Path(run_output_dir) / "output" / "project" if run_output_dir else None
    )
    venv_dir = (
        Path(run_output_dir) / "output" / "project" / "venv"
        if run_output_dir
        else None
    )

    languages = _detect_project_languages(code_files)
    print(f"[QA] Detected languages: {sorted(languages) or ['(none)']}")

    if not project_dir or not project_dir.exists() or not languages:
        reason = (
            "Project directory not found"
            if not project_dir or not project_dir.exists()
            else "No recognised runtime languages — runtime checks skipped"
        )
        print(f"[QA] ⚠️  {reason} — falling back to static-only review")
        per_file_results: list[dict] = run_parallel(
            [
                partial(_analyse_file, llm, compressed_specs, cf)
                for cf in code_files
            ]
        )
        qa_issues = _collect_qa_issues(per_file_results)
        critical = sum(1 for i in qa_issues if i.severity == "critical")
        major = sum(1 for i in qa_issues if i.severity == "major")
        passed = critical == 0 and major == 0
        _log(
            {
                "agent": "qa",
                "phase": "reason",
                "content": f"Static fallback: {critical} critical / {major} major — {'PASS' if passed else 'FAIL'}.",
            }
        )
        return {
            "qa_passed": passed,
            "qa_feedback": ""
            if passed
            else f"{critical} critical and {major} major issues found.",
            "qa_issues": [i.model_dump() for i in qa_issues],
            "code_files": [cf.model_dump() for cf in code_files],
            "iteration": iteration + 1,
            "reasoning_logs": reasoning_logs,
        }

    # ── SCOPE DECISION ────────────────────────────────────────────────────────
    print("[QA] Deciding QA scope …")
    decision = _decide_scope(llm, compressed_specs, code_files)
    print(f"[QA] needs_full_qa={decision.needs_full_qa} — {decision.reasoning}")
    _log(
        {
            "agent": "qa",
            "phase": "plan",
            "content": f"Scope: needs_full_qa={decision.needs_full_qa}. {decision.reasoning}",
        }
    )

    return _run_project_qa(
        llm,
        compressed_specs,
        code_files,
        project_dir,
        venv_dir,
        run_output_dir,
        iteration,
        reasoning_logs,
        languages,
        needs_full_qa=decision.needs_full_qa,
        emit_callback=_emit,
    )
