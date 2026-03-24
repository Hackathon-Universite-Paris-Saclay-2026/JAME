"""QA node — AI-DLC CONSTRUCTION phase / Build and Test stage.

Follows the AI-Driven Development Life Cycle (AI-DLC) methodology:
  Reference: https://github.com/awslabs/aidlc-workflows

AI-DLC stages executed by this node:
  [TRIAGE]    Classify files by risk priority (critical / important / standard)
  [REVIEW]    Per-file static analysis (correctness, security, spec compliance)
  [CROSS]     Cross-file consistency check
  [FIX]       Produce patch instructions or rewrite briefs for Developer node
  [RE-REVIEW] Verify fixes were applied correctly (final iteration only)
  [VERDICT]   Issue final AI-DLC QA PASS / FAIL with audit-ready report

Security rules enforced (AI-DLC security baseline):
  SECURITY-01  Encryption at rest / in transit
  SECURITY-03  Application-level logging (no secrets in logs)
  SECURITY-04  HTTP security headers
  SECURITY-05  Input validation at all API boundaries
  SECURITY-08  Application-level access control (authn/authz)
  SECURITY-09  Security hardening / misconfiguration prevention
  SECURITY-12  Authentication and credential management
  SECURITY-15  Exception handling and fail-safe defaults
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from cancel_token import raise_if_cancelled
from graph.prompts.qa_prompts import (
    CROSS_FILE_PROMPT,
    FULL_REWRITE_PROMPT,
    PATCH_INSTRUCTIONS_PROMPT,
    RE_REVIEW_PROMPT,
    STATIC_ANALYSIS_PROMPT,
    TRIAGE_PROMPT,
    VERDICT_PROMPT,
)
from graph.state import AgentState, CodeFile, QAIssue
from integrations.cortex import get_cortex_llm
from utils.node import parse_json_safe, run_parallel, strip_thinking


# ── Helpers ───────────────────────────────────────────────────────────────────


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


# ── AI-DLC stages ─────────────────────────────────────────────────────────────


def _triage(
    llm: BaseChatModel, specs: str, code_files: list[CodeFile]
) -> dict[str, str]:
    """AI-DLC Triage — classify each file by review priority."""
    print("[TRIAGE] Classifying files by risk priority …")
    file_list = "\n".join(f"- {f.path}" for f in code_files)
    resp = llm.invoke(
        [
            HumanMessage(
                content=TRIAGE_PROMPT.format(
                    specs=specs[:2000], file_list=file_list
                )
            )
        ]
    )
    thinking, raw = strip_thinking(resp.content)
    if thinking:
        print(f"[THINKING — triage]\n{thinking}\n{'─' * 60}")

    result = parse_json_safe(raw, {"files": []})
    priority_map: dict[str, str] = {
        entry["path"]: entry.get("priority", "standard")
        for entry in result.get("files", [])
        if "path" in entry
    }
    for f in code_files:
        priority_map.setdefault(f.path, "standard")

    icons = {"critical": "🔴", "important": "🟡", "standard": "⚪"}
    for path, priority in priority_map.items():
        print(f"    {icons.get(priority, '•')} [{priority:9}] {path}")
    print()
    return priority_map


def _format_tool_output(tool_issues: list[QAIssue]) -> str:
    """Render tool issues as a readable block for prompt injection."""
    if not tool_issues:
        return "No tool issues found."
    return "\n".join(f"- [{i.severity}] {i.description}" for i in tool_issues)


def _analyse_file(
    llm: BaseChatModel,
    specs: str,
    code_file: CodeFile,
    priority: str,
    tool_issues: list[QAIssue] | None = None,
) -> dict:
    """AI-DLC Static Review — single file analysis."""
    print(f"[REVIEW] {code_file.path} [{priority}] …")
    resp = llm.invoke(
        [
            HumanMessage(
                content=STATIC_ANALYSIS_PROMPT.format(
                    priority=priority,
                    specs=specs[:2000],
                    file_path=code_file.path,
                    language=code_file.language,
                    content=code_file.content,
                    tool_output=_format_tool_output(tool_issues or []),
                )
            )
        ]
    )
    thinking, raw = strip_thinking(resp.content)
    if thinking:
        print(f"[THINKING — review {code_file.path}]\n{thinking}\n{'─' * 60}")

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


def _cross_file_check(
    llm: BaseChatModel, specs: str, per_file_results: list[dict]
) -> dict:
    """AI-DLC Cross-file consistency check."""
    print("[CROSS] Running cross-file consistency check …")
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
                    specs=specs[:2000],
                    per_file_summaries="\n".join(summaries),
                )
            )
        ]
    )
    thinking, raw = strip_thinking(resp.content)
    if thinking:
        print(f"[THINKING — cross-file]\n{thinking}\n{'─' * 60}")

    result = parse_json_safe(raw, {"issues": [], "has_issues": False})
    cross_issues = result.get("issues", [])
    if cross_issues:
        print(f"    ⚠️  {len(cross_issues)} cross-file issue(s).")
    else:
        print("    ✅ No cross-file issues.")
    print()
    return result


def _generate_fix_instructions(
    llm: BaseChatModel,
    specs: str,
    code_file: CodeFile,
    issues: list[dict],
    tool_issues: list[QAIssue] | None = None,
) -> str:
    """AI-DLC Fix — patch instructions or full rewrite brief."""
    critical_count = sum(1 for i in issues if i.get("severity") == "critical")

    if critical_count >= 3:
        print(
            f"[FIX] {code_file.path}: {critical_count} critical issues — escalating to rewrite brief."
        )
        issues_text = "\n".join(
            f"- [{i.get('severity', '?')}] {i.get('description', '')} "
            f"(rule: {i.get('security_rule') or 'n/a'}) → {i.get('fix', '')}"
            for i in issues
        )
        resp = llm.invoke(
            [
                HumanMessage(
                    content=FULL_REWRITE_PROMPT.format(
                        file_path=code_file.path,
                        critical_count=critical_count,
                        issues=issues_text,
                        specs=specs[:2000],
                    )
                )
            ]
        )
    else:
        print(f"[FIX] {code_file.path}: generating patch instructions …")
        issues_text = "\n".join(
            f"- [{i.get('severity', '?')}] line {i.get('line_hint', '?')}: "
            f"{i.get('description', '')} "
            f"(rule: {i.get('security_rule') or 'n/a'}) → {i.get('fix', '')}"
            for i in issues
        )
        resp = llm.invoke(
            [
                HumanMessage(
                    content=PATCH_INSTRUCTIONS_PROMPT.format(
                        file_path=code_file.path,
                        tool_output=_format_tool_output(tool_issues or []),
                        issues=issues_text,
                        language=code_file.language,
                        content=code_file.content,
                    )
                )
            ]
        )

    _, instructions = strip_thinking(resp.content)
    return instructions


def _re_review_file(
    llm: BaseChatModel, code_file: CodeFile, original_issues: list[dict]
) -> dict:
    """AI-DLC Re-review — verify fixes on a single file."""
    print(f"[RE-REVIEW] {code_file.path} …")
    original_text = "\n".join(
        f"- [{i.get('severity', '?')}] {i.get('description', '')}"
        for i in original_issues
    )
    resp = llm.invoke(
        [
            HumanMessage(
                content=RE_REVIEW_PROMPT.format(
                    original_issues=original_text,
                    file_path=code_file.path,
                    language=code_file.language,
                    content=code_file.content,
                )
            )
        ]
    )
    thinking, raw = strip_thinking(resp.content)
    if thinking:
        print(
            f"[THINKING — re-review {code_file.path}]\n{thinking}\n{'─' * 60}"
        )

    result = parse_json_safe(
        raw,
        {
            "file": code_file.path,
            "resolved": [],
            "new_issues": [],
            "passed": False,
        },
    )
    print(f"    {'✅ PASS' if result.get('passed') else '❌ FAIL'}")
    return result


def _issue_verdict(
    llm: BaseChatModel, re_review_results: list[dict]
) -> tuple[bool, str]:
    """AI-DLC Verdict — issue final QA PASS / FAIL."""
    file_count = len(re_review_results)
    passed_count = sum(1 for r in re_review_results if r.get("passed"))
    resp = llm.invoke(
        [
            HumanMessage(
                content=VERDICT_PROMPT.format(
                    re_review_results=json.dumps(re_review_results, indent=2),
                    file_count=file_count,
                    passed_count=passed_count,
                    failed_count=file_count - passed_count,
                )
            )
        ]
    )
    _, verdict_text = strip_thinking(resp.content)
    return "AI-DLC QA decision: PASS" in verdict_text, verdict_text


def _collect_qa_issues(
    per_file_results: list[dict], cross_file_result: dict
) -> list[QAIssue]:
    issues: list[QAIssue] = []
    for r in per_file_results:
        for raw in r.get("issues", []):
            issues.append(
                QAIssue(
                    file=r.get("file", "UNKNOWN"),
                    severity=raw.get("severity", "minor"),
                    description=raw.get("description", ""),
                )
            )
    for raw in cross_file_result.get("issues", []):
        issues.append(
            QAIssue(
                file=raw.get("file", "GENERAL"),
                severity=raw.get("severity", "minor"),
                description=raw.get("description", ""),
            )
        )
    return issues


# ── Tool-based checks ─────────────────────────────────────────────────────────


class ToolSkippedError(Exception):
    """Raised when the user chooses to skip a tool — QA stops immediately."""


def _ask_tool(
    tool_call_fn: Callable | None,
    tool_call_id: str,
    tool_name: str,
    command: str,
    args: list[str],
) -> str:
    """Ask the user whether to run or skip a tool.

    Falls back to always-run when no callback is configured (CLI / test mode).
    """
    if tool_call_fn is None:
        return "run"
    return tool_call_fn(tool_call_id, tool_name, command, args)


def _ruff_code_is_blocking(code: str) -> bool:
    """Return True for ruff codes that indicate real correctness/security issues.

    Style-only codes (docstrings, formatting, import order, naming conventions)
    are classified as minor so they don't block QA when tests pass.
    Codes starting with E/W (pycodestyle), D (pydocstyle), ANN (annotations),
    N (naming), Q (quotes), UP (pyupgrade), RET (return), TRY, SIM, etc.
    are style — not blocking.  Codes starting with S (bandit security), B
    (bugbear), F (pyflakes errors), C90 (complexity), and PL (pylint errors)
    are blocking.
    """
    if not code:
        return False
    # Always blocking: security (S), flakes errors (F8xx), bugbear (B), pylint errors (PLE/PLR/PLC)
    blocking_prefixes = ("S", "B", "F8", "F9", "C90", "PLE", "PLR", "PLC")
    for prefix in blocking_prefixes:
        if code.startswith(prefix):
            return True
    # Explicitly not blocking: style/cosmetic
    style_prefixes = ("E", "W", "D", "ANN", "N", "Q", "UP", "RET", "TRY", "SIM", "I", "T", "PT", "ERA")
    for prefix in style_prefixes:
        if code.startswith(prefix):
            return False
    # Default: treat unknown codes as minor to avoid false blocking
    return False


def _run_tool_checks(
    code_files: list[CodeFile],
    project_dir: Path,
    tool_call_fn: Callable | None,
    log: Callable[[dict], None],
) -> list[QAIssue]:
    """Write code files to *project_dir*, then run ruff and pytest.

    For each tool the user is asked Run / Skip before execution:
    - Run  → execute; failures become QAIssue entries.
    - Skip → raises ToolSkippedError, QA stops immediately with FAIL.

    Returns a (possibly empty) list of QAIssue detected by the tools.
    If both tools pass the list is empty and the caller can short-circuit to PASS.
    """
    # Write all files to project_dir so tools can find them
    python_files: list[Path] = []
    test_files: list[Path] = []
    for cf in code_files:
        dest = project_dir / cf.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(cf.content, encoding="utf-8")
        if cf.language == "python" or cf.path.endswith(".py"):
            python_files.append(dest)
            if cf.path.startswith("test") or "/test" in cf.path:
                test_files.append(dest)

    issues: list[QAIssue] = []

    # ── ruff ──────────────────────────────────────────────────────────────────
    ruff_bin = shutil.which("ruff") or sys.executable.replace("python", "ruff")
    ruff_args = ["check", "--output-format=json", str(project_dir)]
    ruff_id = str(uuid.uuid4())

    print(f"\n[TOOL] ruff check {project_dir}")
    action = _ask_tool(tool_call_fn, ruff_id, "ruff", ruff_bin, ruff_args)
    if action == "skip":
        raise ToolSkippedError("ruff")

    log({"agent": "qa", "phase": "act", "content": "Running ruff…"})
    result = subprocess.run(  # noqa: S603
        [ruff_bin, *ruff_args],
        capture_output=True,
        text=True,
        cwd=str(project_dir),
    )
    ruff_output = result.stdout.strip()
    log(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"ruff exit={result.returncode}",
            "tool_result": {
                "tool_name": "ruff",
                "exit_code": result.returncode,
                "output": ruff_output or result.stderr.strip(),
            },
        }
    )

    if result.returncode != 0:
        try:
            diagnostics = json.loads(ruff_output) if ruff_output else []
        except json.JSONDecodeError:
            diagnostics = []

        if diagnostics:
            for d in diagnostics:
                loc = d.get("location", {})
                line = loc.get("row", "?")
                code = d.get("code", "?")
                # Security / correctness rules stay major; pure style/lint → minor
                severity = "major" if _ruff_code_is_blocking(code) else "minor"
                issues.append(
                    QAIssue(
                        file=d.get("filename", "UNKNOWN"),
                        severity=severity,
                        description=f"[ruff] {code} line {line}: {d.get('message', '')}",
                    )
                )
        else:
            # ruff emitted non-JSON (e.g. format warnings) — surface as one issue
            issues.append(
                QAIssue(
                    file=str(project_dir),
                    severity="major",
                    description=f"[ruff] {(ruff_output or result.stderr).strip()[:400]}",
                )
            )
        print(f"    ⚠️  ruff: {len(issues)} issue(s)")
    else:
        print("    ✅ ruff: no issues")

    # ── pytest ─────────────────────────────────────────────────────────────────
    pytest_bin = shutil.which("pytest") or "pytest"
    pytest_args = [str(project_dir), "-v", "--tb=short", "--no-header"]
    pytest_id = str(uuid.uuid4())

    print(f"\n[TOOL] pytest {project_dir}")
    action = _ask_tool(
        tool_call_fn, pytest_id, "pytest", pytest_bin, pytest_args
    )
    if action == "skip":
        raise ToolSkippedError("pytest")

    log({"agent": "qa", "phase": "act", "content": "Running pytest…"})
    result = subprocess.run(  # noqa: S603
        [pytest_bin, *pytest_args],
        capture_output=True,
        text=True,
        cwd=str(project_dir),
    )
    pytest_output = (result.stdout + result.stderr).strip()
    log(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"pytest exit={result.returncode}",
            "tool_result": {
                "tool_name": "pytest",
                "exit_code": result.returncode,
                "output": pytest_output,
            },
        }
    )

    if result.returncode != 0:
        # Extract failed test names from pytest output
        failed_lines = [
            line
            for line in pytest_output.splitlines()
            if line.startswith("FAILED") or "ERROR" in line
        ]
        if failed_lines:
            for line in failed_lines[:20]:
                issues.append(
                    QAIssue(
                        file=str(project_dir),
                        severity="critical",
                        description=f"[pytest] {line.strip()}",
                    )
                )
        else:
            issues.append(
                QAIssue(
                    file=str(project_dir),
                    severity="critical",
                    description=f"[pytest] Tests failed (exit {result.returncode}):\n{pytest_output[:600]}",
                )
            )
        print(f"    ⚠️  pytest: {result.returncode} (failures found)")
    else:
        print("    ✅ pytest: all tests passed")

    return issues


# ── Main node ─────────────────────────────────────────────────────────────────


def qa_node(state: AgentState) -> dict:
    """LangGraph node: AI-DLC CONSTRUCTION phase — Build and Test stage.

    Executes: Triage → Static Review → Cross-file → Fix → Re-review → Verdict

    Args:
        state: Current pipeline state with ``specs``, ``code_files``,
               ``iteration``, and ``max_iterations``.

    Returns:
        A dict updating ``qa_passed``, ``qa_feedback``, ``qa_issues``,
        ``iteration``, and ``reasoning_logs``.
    """
    raise_if_cancelled()
    print("\n" + "=" * 60)
    print("🔍  QUALITY ENGINEER — AI-DLC CONSTRUCTION / Build and Test")
    print("=" * 60)

    specs = state.get("specs", "")
    raw_files = state.get("code_files", [])
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 3)
    # NOTE: `memory` / `maybe_compress` was originally scaffolded here to build
    # a running compressed summary of per-file analysis and fix instructions,
    # intended for injection into the cross-file check and verdict prompts.
    # It was never wired up (memory was accumulated but never passed to any LLM
    # call), and parallelizing REVIEW + FIX makes in-flight accumulation
    # impossible anyway. The structured `per_file_results` and `re_review_results`
    # already give those calls sufficient context — implement as a follow-up if
    # verdict quality degrades on large codebases (10+ files).
    reasoning_logs = []
    _emit = state.get("emit_callback")

    def _log(entry: dict) -> None:
        reasoning_logs.append(entry)
        if _emit:
            _emit(entry)

    _log({"agent": "qa", "phase": "plan", "content": "QA agent starting…"})

    code_files: list[CodeFile] = [_to_code_file(f) for f in raw_files]

    # ── Guard: nothing to review ─────────────────────────────────────────────
    if not code_files:
        print("[AI-DLC] No code files to review — skipping.\n")
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

    print(
        f"\n[AI-DLC] Build and Test — iteration {iteration + 1}/{max_iterations} — {len(code_files)} file(s)"
    )
    _log(
        {
            "agent": "qa",
            "phase": "plan",
            "content": f"Iteration {iteration + 1}/{max_iterations}, {len(code_files)} file(s).",
        }
    )

    # ── [TOOL CHECKS] ruff + pytest before LLM review ────────────────────────
    run_output_dir = state.get("run_output_dir", "")
    tool_call_fn = state.get("tool_call_callback")
    tool_issues: list[QAIssue] = []

    if run_output_dir:
        project_dir = Path(run_output_dir) / "output" / "project"
        try:
            tool_issues = _run_tool_checks(
                code_files, project_dir, tool_call_fn, _log
            )
        except ToolSkippedError as exc:
            print(
                f"\n[AI-DLC] Tool '{exc}' skipped by user — stopping QA with FAIL.\n"
            )
            _log(
                {
                    "agent": "qa",
                    "phase": "reason",
                    "content": f"Tool '{exc}' skipped by user. AI-DLC QA decision: FAIL.",
                }
            )
            return {
                "qa_passed": False,
                "qa_feedback": f"QA stopped: user skipped the '{exc}' tool check.",
                "qa_issues": [],
                "iteration": iteration + 1,
                "reasoning_logs": reasoning_logs,
            }

        if not tool_issues:
            # All tool checks passed — skip LLM pipeline entirely
            print("\n[AI-DLC] All tool checks passed — skipping LLM review.\n")
            _log(
                {
                    "agent": "qa",
                    "phase": "reason",
                    "content": "ruff + pytest both passed. AI-DLC QA decision: PASS.",
                }
            )
            return {
                "qa_passed": True,
                "qa_feedback": "",
                "qa_issues": [],
                "iteration": iteration + 1,
                "reasoning_logs": reasoning_logs,
            }

        # Tool failures found — feed them into the LLM review as pre-seeded issues
        print(
            f"\n[AI-DLC] Tool checks found {len(tool_issues)} issue(s) — continuing with LLM review.\n"
        )
        _log(
            {
                "agent": "qa",
                "phase": "act",
                "content": f"Tool checks: {len(tool_issues)} issue(s). Continuing to LLM review.",
            }
        )

    # ── [TRIAGE] ─────────────────────────────────────────────────────────────
    priority_map = _triage(llm, specs, code_files)
    _log(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"Triage complete: {priority_map}",
        }
    )

    # ── [REVIEW] All files in parallel (critical-first order preserved) ───────
    # Build a per-file index of tool issues so REVIEW and FIX see exact diagnostics.
    # ruff emits absolute paths; match on basename as a fallback.
    tool_issues_by_file: dict[str, list[QAIssue]] = {}
    for issue in tool_issues:
        for cf in code_files:
            if cf.path in issue.file or issue.file.endswith(cf.path):
                tool_issues_by_file.setdefault(cf.path, []).append(issue)
                break

    ordered_files = sorted(
        code_files,
        key=lambda f: {"critical": 0, "important": 1, "standard": 2}.get(
            priority_map.get(f.path, "standard"), 2
        ),
    )
    per_file_results: list[dict] = run_parallel(
        [
            partial(
                _analyse_file,
                llm,
                specs,
                f,
                priority_map.get(f.path, "standard"),
                tool_issues_by_file.get(f.path, []),
            )
            for f in ordered_files
        ]
    )

    _log(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"{len(per_file_results)} file(s) analysed.",
        }
    )

    # ── [CROSS] ──────────────────────────────────────────────────────────────
    cross_file_result = _cross_file_check(llm, specs, per_file_results)
    _log(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"{len(cross_file_result.get('issues', []))} cross-file issue(s).",
        }
    )

    all_qa_issues = _collect_qa_issues(per_file_results, cross_file_result)
    critical_total = sum(1 for i in all_qa_issues if i.severity == "critical")
    major_total = sum(1 for i in all_qa_issues if i.severity == "major")
    minor_total = len(all_qa_issues) - critical_total - major_total

    print(
        f"[AI-DLC] Total issues: {len(all_qa_issues)} ({critical_total} critical / {major_total} major / {minor_total} minor)"
    )

    # ── Early exit: only minor issues → immediate PASS ───────────────────────
    if critical_total == 0 and major_total == 0:
        _print_approval_gate(
            stage="Build and Test",
            summary_lines=[
                f"{len(code_files)} file(s) reviewed",
                f"{minor_total} minor issue(s) — no blocking issues",
                "AI-DLC security rules: all compliant",
            ],
            next_stage="Pipeline complete",
        )
        _log(
            {
                "agent": "qa",
                "phase": "reason",
                "content": "No critical/major issues. AI-DLC QA decision: PASS.",
            }
        )
        return {
            "qa_passed": True,
            "qa_feedback": "",
            "qa_issues": [i.model_dump() for i in all_qa_issues],
            "iteration": iteration + 1,
            "reasoning_logs": reasoning_logs,
        }

    # ── [FIX] All files with issues in parallel ───────────────────────────────
    files_to_fix: list[tuple[dict, CodeFile]] = []
    for result in per_file_results:
        if not result.get("issues"):
            continue
        code_file = next(
            (f for f in code_files if f.path == result["file"]), None
        )
        if code_file is not None:
            files_to_fix.append((result, code_file))

    print(
        f"[FIX] Generating fix instructions for {len(files_to_fix)} file(s) …"
    )
    _log(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"Generating fix instructions for {len(files_to_fix)} file(s)…",
        }
    )

    instructions_list: list[str] = run_parallel(
        [
            partial(
                _generate_fix_instructions,
                llm,
                specs,
                code_file,
                result["issues"],
                tool_issues_by_file.get(code_file.path, []),
            )
            for result, code_file in files_to_fix
        ]
    )
    fix_feedback_parts = [
        f"### {code_file.path}\n{instructions}"
        for (_, code_file), instructions in zip(
            files_to_fix, instructions_list, strict=True
        )
    ]

    cross_issues = cross_file_result.get("issues", [])
    if cross_issues:
        cross_text = "\n".join(
            f"- [{i.get('severity')}] {i.get('file', 'GENERAL')}: {i.get('description', '')} → {i.get('fix', '')}"
            for i in cross_issues
        )
        fix_feedback_parts.append(f"### Cross-file issues\n{cross_text}")

    qa_feedback = "\n\n".join(fix_feedback_parts)
    _log(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"Fix instructions produced for {len(fix_feedback_parts)} target(s).",
        }
    )

    # ── Return to Developer if iterations remain ──────────────────────────────
    if iteration + 1 < max_iterations:
        # Senior mode: pause and ask the human before routing back to developer.
        # This lets them review QA findings and optionally inject instructions.
        mode = state.get("mode", "senior")
        clarification_callback = state.get("clarification_callback")
        if mode == "senior" and clarification_callback is not None:
            _summary_lines = "\n".join(
                f"- [{i.severity}] {i.file}: {i.description[:120]}"
                for i in all_qa_issues[:8]
            )
            _review_prompt = (
                "QA iteration review: "  # sentinel prefix for service routing
                f"QA found {len(all_qa_issues)} issue(s) "
                f"({critical_total} critical / {major_total} major / {minor_total} minor).\n\n"
                f"{_summary_lines}\n\n"
                "Reply 'proceed' to let the developer fix these automatically, "
                "or type additional instructions to inject."
            )
            _log(
                {
                    "agent": "qa",
                    "phase": "iteration_review",
                    "content": f"Senior review checkpoint: {len(all_qa_issues)} issue(s) found. Waiting for human.",
                }
            )
            human_input = (clarification_callback(_review_prompt, ["proceed | Let developer fix automatically"]) or "").strip()
            if human_input and human_input.lower() not in ("proceed", ""):
                # Queue the instruction for the developer's next iteration
                _log(
                    {
                        "agent": "qa",
                        "phase": "iteration_review",
                        "content": f"Senior instruction queued: {human_input[:120]}",
                    }
                )
                return {
                    "qa_passed": False,
                    "qa_feedback": qa_feedback,
                    "qa_issues": [i.model_dump() for i in all_qa_issues],
                    "iteration": iteration + 1,
                    "senior_prompt_queue": [human_input],
                    "reasoning_logs": reasoning_logs,
                }

        print(
            f"[AI-DLC] Fix instructions dispatched to Developer (iteration {iteration + 1}/{max_iterations}).\n"
        )
        _log(
            {
                "agent": "qa",
                "phase": "reason",
                "content": f"Issues found — routing back to Developer for fixes (iteration {iteration + 1}/{max_iterations}).",
            }
        )
        return {
            "qa_passed": False,
            "qa_feedback": qa_feedback,
            "qa_issues": [i.model_dump() for i in all_qa_issues],
            "iteration": iteration + 1,
            "reasoning_logs": reasoning_logs,
        }

    # ── [RE-REVIEW] Final iteration ───────────────────────────────────────────
    print(
        "[AI-DLC] Max iterations reached — re-reviewing and issuing verdict.\n"
    )
    re_review_results: list[dict] = []
    files_to_rereview: list[tuple[dict, CodeFile]] = []
    for result in per_file_results:
        if not result.get("issues"):
            re_review_results.append(
                {
                    "file": result["file"],
                    "resolved": [],
                    "new_issues": [],
                    "passed": True,
                }
            )
            continue
        code_file = next(
            (f for f in code_files if f.path == result["file"]), None
        )
        if code_file is not None:
            files_to_rereview.append((result, code_file))

    re_review_results += run_parallel(
        [
            partial(_re_review_file, llm, code_file, result["issues"])
            for result, code_file in files_to_rereview
        ]
    )

    # ── [VERDICT] ─────────────────────────────────────────────────────────────
    passed, verdict_text = _issue_verdict(llm, re_review_results)
    print("\n" + "=" * 60)
    print(verdict_text)
    print("=" * 60 + "\n")

    _log(
        {
            "agent": "qa",
            "phase": "reason",
            "content": f"AI-DLC QA decision: {'PASS' if passed else 'FAIL'} after {iteration + 1} iteration(s).",
        }
    )
    return {
        "qa_passed": passed,
        "qa_feedback": "" if passed else qa_feedback,
        "qa_issues": [i.model_dump() for i in all_qa_issues],
        "iteration": iteration + 1,
        "reasoning_logs": reasoning_logs,
    }
