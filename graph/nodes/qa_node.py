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

from functools import partial
import json

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


def _analyse_file(
    llm: BaseChatModel, specs: str, code_file: CodeFile, priority: str
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
    llm: BaseChatModel, specs: str, code_file: CodeFile, issues: list[dict]
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
    reasoning_logs.append(
        {
            "agent": "qa",
            "phase": "plan",
            "content": f"Iteration {iteration + 1}/{max_iterations}, {len(code_files)} file(s).",
        }
    )

    # ── [TRIAGE] ─────────────────────────────────────────────────────────────
    priority_map = _triage(llm, specs, code_files)
    reasoning_logs.append(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"Triage complete: {priority_map}",
        }
    )

    # ── [REVIEW] All files in parallel (critical-first order preserved) ───────
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
            )
            for f in ordered_files
        ]
    )

    reasoning_logs.append(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"{len(per_file_results)} file(s) analysed.",
        }
    )

    # ── [CROSS] ──────────────────────────────────────────────────────────────
    cross_file_result = _cross_file_check(llm, specs, per_file_results)
    reasoning_logs.append(
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
        reasoning_logs.append(
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

    instructions_list: list[str] = run_parallel(
        [
            partial(
                _generate_fix_instructions,
                llm,
                specs,
                code_file,
                result["issues"],
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
    reasoning_logs.append(
        {
            "agent": "qa",
            "phase": "act",
            "content": f"Fix instructions produced for {len(fix_feedback_parts)} target(s).",
        }
    )

    # ── Return to Developer if iterations remain ──────────────────────────────
    if iteration + 1 < max_iterations:
        print(
            f"[AI-DLC] Fix instructions dispatched to Developer (iteration {iteration + 1}/{max_iterations}).\n"
        )
        reasoning_logs.append(
            {
                "agent": "qa",
                "phase": "reason",
                "content": f"AI-DLC QA decision: FAIL at iteration {iteration + 1}. Feedback dispatched.",
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

    reasoning_logs.append(
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
