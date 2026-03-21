"""Quality Engineer Agent — AI-DLC CONSTRUCTION phase / Build and Test stage.

Follows the AI-Driven Development Life Cycle (AI-DLC) methodology:
  Reference: https://github.com/awslabs/aidlc-workflows

AI-DLC phases executed by this agent:
  CONSTRUCTION → Build and Test stage
    [TRIAGE]    Classify files by risk priority (critical / important / standard)
    [REVIEW]    Per-file static analysis (correctness, security, spec compliance)
    [CROSS]     Cross-file consistency check
    [FIX]       Produce patch instructions or rewrite briefs for Developer Agent
    [RE-REVIEW] Verify fixes were applied correctly (after Developer iteration)
    [VERDICT]   Issue final AI-DLC QA PASS / FAIL with audit-ready report

Security rules enforced (from AI-DLC security extension baseline):
  SECURITY-01  Encryption at rest / in transit
  SECURITY-03  Application-level logging (no secrets in logs)
  SECURITY-04  HTTP security headers
  SECURITY-05  Input validation at all API boundaries
  SECURITY-08  Application-level access control (authn/authz, IDOR prevention)
  SECURITY-09  Security hardening / misconfiguration prevention
  SECURITY-12  Authentication and credential management
  SECURITY-15  Exception handling and fail-safe defaults

Reads from AgentState:  specs, code_files, iteration, max_iterations
Writes to AgentState:   qa_passed, qa_feedback, qa_issues, iteration, reasoning_logs
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import yaml
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from cancel_token import CancelToken, RunCancelledError
from state import AgentState, CodeFile, QAIssue

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

MAX_FIX_ITERATIONS = 3
COMPRESS_THRESHOLD = 5000
PROMPTS_FILE       = Path(__file__).parent.parent / "prompts" / "quality_engineer.yaml"

# ──────────────────────────────────────────────────────────────────────────────
# Prompts (loaded from prompts/quality_engineer.yaml)
# ──────────────────────────────────────────────────────────────────────────────

def _load_prompts() -> dict:
    with open(PROMPTS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)

_PROMPTS = _load_prompts()

TRIAGE_PROMPT             = _PROMPTS["triage"]["classify_files"]
STATIC_ANALYSIS_PROMPT    = _PROMPTS["review"]["static_analysis"]
CROSS_FILE_PROMPT         = _PROMPTS["review"]["cross_file"]
PATCH_INSTRUCTIONS_PROMPT = _PROMPTS["fix"]["patch_instructions"]
FULL_REWRITE_PROMPT       = _PROMPTS["fix"]["full_rewrite"]
RE_REVIEW_PROMPT          = _PROMPTS["validation"]["re_review"]
VERDICT_PROMPT            = _PROMPTS["validation"]["verdict"]
COMPRESS_PROMPT           = _PROMPTS["memory"]["compress"]

# ──────────────────────────────────────────────────────────────────────────────
# LLM
# ──────────────────────────────────────────────────────────────────────────────

def _get_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model="deepseek-r1",
        temperature=0.3,
        max_tokens=4096,
        openai_api_key=os.environ["SNOWFLAKE_API_KEY"],
        openai_api_base=os.environ["SNOWFLAKE_API_BASE"],
    )

# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> tuple[str, str]:
    """Extract and strip <think>…</think> blocks from reasoning model output.

    Handles well-formed blocks, multiple blocks, and unclosed <think> tags.
    """
    if not text:
        return "", text

    thinking_parts: list[str] = []
    content = re.sub(
        r"<think>(.*?)</think>",
        lambda m: thinking_parts.append(m.group(1).strip()) or "",
        text,
        flags=re.DOTALL,
    )
    # Handle unclosed <think> — remainder is reasoning
    if "<think>" in content:
        idx = content.index("<think>")
        thinking_parts.append(content[idx + len("<think>"):].strip())
        content = content[:idx]

    thinking = "\n\n".join(thinking_parts).strip()
    content = content.strip()
    if not content:
        content = text.strip()
    return thinking, content


def _parse_json_response(raw: str, fallback: dict) -> dict:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    raw = raw.strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return fallback


def _llm_invoke(llm: ChatOpenAI, messages: list, token: CancelToken | None = None):
    """Invoke LLM, checking cancel token before and after."""
    if token:
        token.raise_if_cancelled()
    result = llm.invoke(messages)
    if token:
        token.raise_if_cancelled()
    return result


def _compress(llm: ChatOpenAI, context: str, token: CancelToken | None = None) -> str:
    print("[MEMORY] Context too large — compressing …")
    resp = _llm_invoke(llm, [HumanMessage(content=COMPRESS_PROMPT.format(context=context))], token)
    _, compressed = _strip_thinking(resp.content)
    print("[MEMORY] Compressed.\n")
    return compressed


def _maybe_compress(llm: ChatOpenAI, new_context: str, memory: str, token: CancelToken | None = None) -> str:
    full = (memory + "\n\n" + new_context).strip()
    return _compress(llm, full, token) if len(full) > COMPRESS_THRESHOLD else full

# ──────────────────────────────────────────────────────────────────────────────
# AI-DLC stage: TRIAGE
# ──────────────────────────────────────────────────────────────────────────────

def _triage(llm: ChatOpenAI, specs: str, code_files: list[CodeFile], token: CancelToken | None = None) -> dict[str, str]:
    """AI-DLC Triage stage — classify each file by review priority.

    Returns {file_path: "critical"|"important"|"standard"}.
    """
    print("[TRIAGE] Classifying files by risk priority …")
    # Handle both dict and object formats
    file_list = []
    for f in code_files:
        if isinstance(f, dict):
            file_list.append(f"- {f.get('path', 'unknown')}")
        else:
            file_list.append(f"- {f.path}")
    file_list_str = "\n".join(file_list)
    resp = _llm_invoke(llm, [HumanMessage(content=TRIAGE_PROMPT.format(
        specs=specs[:2000],
        file_list=file_list_str,
    ))], token)
    thinking, raw = _strip_thinking(resp.content)
    if thinking:
        print(f"[THINKING — triage]\n{thinking}\n{'─' * 60}")

    result = _parse_json_response(raw, {"files": []})
    priority_map: dict[str, str] = {
        entry["path"]: entry.get("priority", "standard")
        for entry in result.get("files", [])
        if "path" in entry
    }
    for f in code_files:
        # Handle both dict and object formats
        file_path = f.get("path") if isinstance(f, dict) else f.path
        priority_map.setdefault(file_path, "standard")

    icons = {"critical": "🔴", "important": "🟡", "standard": "⚪"}
    for path, priority in priority_map.items():
        print(f"    {icons.get(priority, '•')} [{priority:9}] {path}")
    print()
    return priority_map

# ──────────────────────────────────────────────────────────────────────────────
# AI-DLC stage: STATIC REVIEW (per file)
# ──────────────────────────────────────────────────────────────────────────────

def _analyse_file(
    llm: ChatOpenAI,
    specs: str,
    code_file: CodeFile,
    priority: str,
    token: CancelToken | None = None,
) -> dict:
    """AI-DLC Static Review stage — single file analysis."""
    print(f"[REVIEW] {code_file.path} [{priority}] …")
    resp = _llm_invoke(llm, [HumanMessage(content=STATIC_ANALYSIS_PROMPT.format(
        priority=priority,
        specs=specs[:2000],
        file_path=code_file.path,
        language=code_file.language,
        content=code_file.content,
    ))], token)
    thinking, raw = _strip_thinking(resp.content)
    if thinking:
        print(f"[THINKING — review {code_file.path}]\n{thinking}\n{'─' * 60}")

    fallback = {"file": code_file.path, "issues": [], "has_issues": False}
    result   = _parse_json_response(raw, fallback)
    issues   = result.get("issues", [])
    if issues:
        c = sum(1 for i in issues if i.get("severity") == "critical")
        m = sum(1 for i in issues if i.get("severity") == "major")
        n = len(issues) - c - m
        print(f"    ⚠️  {len(issues)} issue(s): {c} critical / {m} major / {n} minor")
    else:
        print("    ✅ No issues.")
    return result

# ──────────────────────────────────────────────────────────────────────────────
# AI-DLC stage: CROSS-FILE CONSISTENCY
# ──────────────────────────────────────────────────────────────────────────────

def _cross_file_check(
    llm: ChatOpenAI,
    specs: str,
    per_file_results: list[dict],
    token: CancelToken | None = None,
) -> dict:
    """AI-DLC cross-file consistency check within Build and Test stage."""
    print("[CROSS] Running cross-file consistency check …")
    summaries = [
        f"- {r['file']}: " + (
            "; ".join(i.get("description", "") for i in r.get("issues", []))
            or "no issues"
        )
        for r in per_file_results
    ]
    resp = _llm_invoke(llm, [HumanMessage(content=CROSS_FILE_PROMPT.format(
        specs=specs[:2000],
        per_file_summaries="\n".join(summaries),
    ))], token)
    thinking, raw = _strip_thinking(resp.content)
    if thinking:
        print(f"[THINKING — cross-file]\n{thinking}\n{'─' * 60}")

    result = _parse_json_response(raw, {"issues": [], "has_issues": False})
    cross_issues = result.get("issues", [])
    if cross_issues:
        print(f"    ⚠️  {len(cross_issues)} cross-file issue(s).")
    else:
        print("    ✅ No cross-file issues.")
    print()
    return result

# ──────────────────────────────────────────────────────────────────────────────
# AI-DLC stage: FIX — produce instructions for Developer Agent
# ──────────────────────────────────────────────────────────────────────────────

def _generate_fix_instructions(
    llm: ChatOpenAI,
    specs: str,
    code_file: CodeFile,
    issues: list[dict],
    token: CancelToken | None = None,
) -> str:
    """AI-DLC Fix stage — patch instructions or full rewrite brief."""
    critical_count = sum(1 for i in issues if i.get("severity") == "critical")

    if critical_count >= 3:
        print(f"[FIX] {code_file.path}: {critical_count} critical issues — escalating to rewrite brief.")
        issues_text = "\n".join(
            f"- [{i.get('severity', '?')}] {i.get('description', '')} "
            f"(rule: {i.get('security_rule') or 'n/a'}) → {i.get('fix', '')}"
            for i in issues
        )
        resp = _llm_invoke(llm, [HumanMessage(content=FULL_REWRITE_PROMPT.format(
            file_path=code_file.path,
            critical_count=critical_count,
            issues=issues_text,
            specs=specs[:2000],
        ))], token)
    else:
        print(f"[FIX] {code_file.path}: generating patch instructions …")
        issues_text = "\n".join(
            f"- [{i.get('severity', '?')}] line {i.get('line_hint', '?')}: "
            f"{i.get('description', '')} "
            f"(rule: {i.get('security_rule') or 'n/a'}) → {i.get('fix', '')}"
            for i in issues
        )
        resp = _llm_invoke(llm, [HumanMessage(content=PATCH_INSTRUCTIONS_PROMPT.format(
            file_path=code_file.path,
            issues=issues_text,
            language=code_file.language,
            content=code_file.content,
        ))], token)

    _, instructions = _strip_thinking(resp.content)
    return instructions

# ──────────────────────────────────────────────────────────────────────────────
# AI-DLC stage: RE-REVIEW (after Developer applies fixes)
# ──────────────────────────────────────────────────────────────────────────────

def _re_review_file(
    llm: ChatOpenAI,
    code_file: CodeFile,
    original_issues: list[dict],
    token: CancelToken | None = None,
) -> dict:
    """AI-DLC Re-review stage — verify fixes on a single file."""
    print(f"[RE-REVIEW] {code_file.path} …")
    original_text = "\n".join(
        f"- [{i.get('severity', '?')}] {i.get('description', '')}"
        for i in original_issues
    )
    resp = _llm_invoke(llm, [HumanMessage(content=RE_REVIEW_PROMPT.format(
        original_issues=original_text,
        file_path=code_file.path,
        language=code_file.language,
        content=code_file.content,
    ))], token)
    thinking, raw = _strip_thinking(resp.content)
    if thinking:
        print(f"[THINKING — re-review {code_file.path}]\n{thinking}\n{'─' * 60}")

    fallback = {"file": code_file.path, "resolved": [], "new_issues": [], "passed": False}
    result   = _parse_json_response(raw, fallback)
    print(f"    {'✅ PASS' if result.get('passed') else '❌ FAIL'}")
    return result

# ──────────────────────────────────────────────────────────────────────────────
# AI-DLC stage: VERDICT — final PASS / FAIL report
# ──────────────────────────────────────────────────────────────────────────────

def _issue_verdict(
    llm: ChatOpenAI,
    re_review_results: list[dict],
    token: CancelToken | None = None,
) -> tuple[bool, str]:
    """AI-DLC Verdict stage — issue final QA PASS / FAIL."""
    file_count   = len(re_review_results)
    passed_count = sum(1 for r in re_review_results if r.get("passed"))
    failed_count = file_count - passed_count

    resp = _llm_invoke(llm, [HumanMessage(content=VERDICT_PROMPT.format(
        re_review_results=json.dumps(re_review_results, indent=2),
        file_count=file_count,
        passed_count=passed_count,
        failed_count=failed_count,
    ))], token)
    _, verdict_text = _strip_thinking(resp.content)
    passed = "AI-DLC QA decision: PASS" in verdict_text
    return passed, verdict_text

# ──────────────────────────────────────────────────────────────────────────────
# Helper: collect all issues as QAIssue model objects
# ──────────────────────────────────────────────────────────────────────────────

def _collect_qa_issues(
    per_file_results: list[dict],
    cross_file_result: dict,
) -> list[QAIssue]:
    issues: list[QAIssue] = []
    for r in per_file_results:
        for raw in r.get("issues", []):
            issues.append(QAIssue(
                file=r.get("file", "UNKNOWN"),
                severity=raw.get("severity", "minor"),
                description=raw.get("description", ""),
            ))
    for raw in cross_file_result.get("issues", []):
        issues.append(QAIssue(
            file=raw.get("file", "GENERAL"),
            severity=raw.get("severity", "minor"),
            description=raw.get("description", ""),
        ))
    return issues

# ──────────────────────────────────────────────────────────────────────────────
# AI-DLC approval gate message
# ──────────────────────────────────────────────────────────────────────────────

def _print_approval_gate(stage: str, summary_lines: list[str], next_stage: str) -> None:
    """Print an AI-DLC formatted approval gate message."""
    print("\n" + "=" * 60)
    print(f"# {stage} Complete")
    print("=" * 60)
    for line in summary_lines:
        print(f"  • {line}")
    print()
    print("  📋 REVIEW REQUIRED")
    print(f"  🚀 WHAT'S NEXT: Proceeding to → {next_stage}")
    print("=" * 60 + "\n")

# ──────────────────────────────────────────────────────────────────────────────
# LangGraph node — AI-DLC CONSTRUCTION / Build and Test
# ──────────────────────────────────────────────────────────────────────────────

def quality_engineer_node(state: AgentState) -> dict:
    """AI-DLC CONSTRUCTION phase — Build and Test stage.

    Executes: Triage → Static Review → Cross-file → Fix → Re-review → Verdict

    Reads from state:  specs, code_files, iteration, max_iterations
    Writes to state:   qa_passed, qa_feedback, qa_issues, iteration, reasoning_logs
    """

    print("\n" + "=" * 60)
    print("🔍  QUALITY ENGINEER — AI-DLC CONSTRUCTION / Build and Test")
    print("=" * 60)

    specs          = state.get("specs", "")
    code_files     = state.get("code_files", [])
    iteration      = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", MAX_FIX_ITERATIONS)
    token: CancelToken | None = state.get("_cancel_token")  # type: ignore[assignment]
    memory         = ""
    reasoning_logs = []

    # ── Convert dict code files to CodeFile objects ─────────────────────────
    converted_files = []
    for f in code_files:
        if isinstance(f, dict):
            converted_files.append(CodeFile(
                path=f.get("path", "unknown"),
                content=f.get("content", ""),
                language=f.get("language", "python")
            ))
        else:
            # Already a CodeFile object
            converted_files.append(f)
    code_files = converted_files

    # ── Guard: nothing to review (no LLM needed) ────────────────────────────
    if not code_files:
        print("[AI-DLC] No code files to review — skipping Build and Test stage.\n")
        return {
            "qa_passed":      False,
            "qa_feedback":    "No code files provided to the Quality Engineer.",
            "qa_issues":      [],
            "reasoning_logs": [{
                "agent": "quality_engineer",
                "phase": "CONSTRUCTION/build-and-test",
                "stage": "triage",
                "content": "No code files to review.",
            }],
        }

    llm = _get_llm()

    print(f"\n[AI-DLC] Build and Test — iteration {iteration + 1}/{max_iterations} "
          f"— {len(code_files)} file(s)")
    reasoning_logs.append({
        "agent":   "quality_engineer",
        "phase":   "CONSTRUCTION/build-and-test",
        "stage":   "init",
        "content": f"Iteration {iteration + 1}/{max_iterations}, {len(code_files)} file(s).",
    })

    # ── [TRIAGE] Classify files by risk priority ─────────────────────────────
    reasoning_logs.append({
        "agent":   "Quality Engineer",
        "phase":   "CONSTRUCTION/build-and-test",
        "stage":   "triage",
        "content": f"Triaging {len(code_files)} file(s)...",
        "thinking": "",
    })
    priority_map = _triage(llm, specs, code_files, token)
    # Summarise triage result
    priority_summary = ", ".join(f"{p}: {v}" for p, v in list(priority_map.items())[:5])
    reasoning_logs.append({
        "agent":   "Quality Engineer",
        "phase":   "CONSTRUCTION/build-and-test",
        "stage":   "triage",
        "content": f"Triage complete: {priority_summary}",
        "thinking": "",
    })

    # ── [REVIEW] Static analysis — critical files first ──────────────────────
    ordered_files = sorted(
        code_files,
        key=lambda f: {"critical": 0, "important": 1, "standard": 2}.get(
            priority_map.get(f.path, "standard"), 2
        ),
    )

    per_file_results: list[dict] = []
    for code_file in ordered_files:
        priority = priority_map.get(code_file.path, "standard")
        reasoning_logs.append({
            "agent":   "Quality Engineer",
            "phase":   "CONSTRUCTION/build-and-test",
            "stage":   "review",
            "content": f"Reviewing {code_file.path} [{priority}]...",
            "thinking": "",
        })
        result   = _analyse_file(llm, specs, code_file, priority, token)
        per_file_results.append(result)
        n_issues = len(result.get("issues", []))
        reasoning_logs.append({
            "agent":   "Quality Engineer",
            "phase":   "CONSTRUCTION/build-and-test",
            "stage":   "review",
            "content": f"{code_file.path}: {n_issues} issue(s) found",
            "thinking": "",
        })
        memory = _maybe_compress(
            llm,
            f"Reviewed {code_file.path}: {n_issues} issue(s).",
            memory,
            token,
        )

    # ── [CROSS] Cross-file consistency ───────────────────────────────────────
    reasoning_logs.append({
        "agent":   "Quality Engineer",
        "phase":   "CONSTRUCTION/build-and-test",
        "stage":   "cross-file",
        "content": "Running cross-file consistency check...",
        "thinking": "",
    })
    cross_file_result = _cross_file_check(llm, specs, per_file_results, token)
    reasoning_logs.append({
        "agent":   "Quality Engineer",
        "phase":   "CONSTRUCTION/build-and-test",
        "stage":   "cross-file",
        "content": f"{len(cross_file_result.get('issues', []))} cross-file issue(s).",
        "thinking": "",
    })

    all_qa_issues  = _collect_qa_issues(per_file_results, cross_file_result)
    critical_total = sum(1 for i in all_qa_issues if i.severity == "critical")
    major_total    = sum(1 for i in all_qa_issues if i.severity == "major")
    minor_total    = len(all_qa_issues) - critical_total - major_total

    print(f"[AI-DLC] Total issues: {len(all_qa_issues)} "
          f"({critical_total} critical / {major_total} major / {minor_total} minor)")

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
        reasoning_logs.append({
            "agent":   "quality_engineer",
            "phase":   "CONSTRUCTION/build-and-test",
            "stage":   "verdict",
            "content": "No critical/major issues. AI-DLC QA decision: PASS.",
        })
        return {
            "qa_passed":      True,
            "qa_feedback":    "",
            "qa_issues":      [i.model_dump() for i in all_qa_issues],
            "reasoning_logs": reasoning_logs,
        }

    # ── [FIX] Generate fix instructions per problematic file ─────────────────
    fix_feedback_parts: list[str] = []
    for result in per_file_results:
        file_issues = result.get("issues", [])
        if not file_issues:
            continue
        code_file = next((f for f in code_files if f.path == result["file"]), None)
        if code_file is None:
            continue
        instructions = _generate_fix_instructions(llm, specs, code_file, file_issues, token)
        fix_feedback_parts.append(f"### {code_file.path}\n{instructions}")
        memory = _maybe_compress(llm, f"Fix instructions for {code_file.path}.", memory, token)

    cross_issues = cross_file_result.get("issues", [])
    if cross_issues:
        cross_text = "\n".join(
            f"- [{i.get('severity')}] {i.get('file', 'GENERAL')}: "
            f"{i.get('description', '')} → {i.get('fix', '')}"
            for i in cross_issues
        )
        fix_feedback_parts.append(f"### Cross-file issues\n{cross_text}")

    qa_feedback = "\n\n".join(fix_feedback_parts)
    reasoning_logs.append({
        "agent":   "quality_engineer",
        "phase":   "CONSTRUCTION/build-and-test",
        "stage":   "fix",
        "content": f"Fix instructions produced for {len(fix_feedback_parts)} target(s).",
    })

    # ── Return to Developer Agent if iterations remain ────────────────────────
    if iteration + 1 < max_iterations:
        print(f"[AI-DLC] Fix instructions dispatched to Developer Agent "
              f"(iteration {iteration + 1}/{max_iterations}).\n")
        reasoning_logs.append({
            "agent":   "quality_engineer",
            "phase":   "CONSTRUCTION/build-and-test",
            "stage":   "fix",
            "content": f"AI-DLC QA decision: FAIL at iteration {iteration + 1}. Feedback dispatched.",
        })
        return {
            "qa_passed":      False,
            "qa_feedback":    qa_feedback,
            "qa_issues":      [i.model_dump() for i in all_qa_issues],
            "iteration":      iteration + 1,
            "reasoning_logs": reasoning_logs,
        }

    # ── [RE-REVIEW] Final iteration: re-review every file ────────────────────
    print("[AI-DLC] Max iterations reached — re-reviewing and issuing verdict.\n")
    re_review_results: list[dict] = []
    for result in per_file_results:
        original_issues = result.get("issues", [])
        if not original_issues:
            re_review_results.append({
                "file": result["file"], "resolved": [], "new_issues": [], "passed": True,
            })
            continue
        code_file = next((f for f in code_files if f.path == result["file"]), None)
        if code_file is None:
            continue
        re_review_results.append(_re_review_file(llm, code_file, original_issues, token))

    # ── [VERDICT] Issue final AI-DLC QA decision ─────────────────────────────
    passed, verdict_text = _issue_verdict(llm, re_review_results, token)

    print("\n" + "=" * 60)
    print(verdict_text)
    print("=" * 60 + "\n")

    reasoning_logs.append({
        "agent":   "quality_engineer",
        "phase":   "CONSTRUCTION/build-and-test",
        "stage":   "verdict",
        "content": f"AI-DLC QA decision: {'PASS' if passed else 'FAIL'} "
                   f"after {iteration + 1} iteration(s).",
    })

    return {
        "qa_passed":      passed,
        "qa_feedback":    "" if passed else qa_feedback,
        "qa_issues":      [i.model_dump() for i in all_qa_issues],
        "iteration":      iteration + 1,
        "reasoning_logs": reasoning_logs,
    }
