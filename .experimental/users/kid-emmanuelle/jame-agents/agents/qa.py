"""QA Agent — Validates generated code against the specifications.

Returns structured "actionable tickets" (List[QAIssue]) instead of
free-form text so the Developer agent can address issues precisely.
"""

from __future__ import annotations

import os

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from state import AgentState, QAResult

# Minimum files the Developer MUST produce
REQUIRED_FILES = [
    "backend/main.py",
    "backend/models.py",
    "tests/test_main.py",
    "frontend/src/App.js",
]

SYSTEM_PROMPT = """\
You are the **QA Agent** of a multi-agent software factory.

## Role
You receive the original specifications AND the generated code files.
Your job is to validate that the code correctly and completely implements
the specifications.

## Checks to perform
1. **Completeness**: Are all specified modules, routes, and models present?
   The following files are MANDATORY:
   - backend/main.py (FastAPI app with all routes)
   - backend/models.py (data models)
   - tests/test_main.py (pytest tests)
   - frontend/src/App.js (React UI)
   - A requirements.txt or package.json as appropriate
2. **Correctness**: Do the implementations match the spec (correct HTTP methods,
   field types, validation rules)?
3. **Test Coverage**: Are there tests for every route / major function?
4. **Code Quality**: Are there obvious bugs, missing imports, or syntax errors?
5. **Runnability**: Could someone actually run this code as-is?

## Output rules
- Set `passed` to true ONLY if every check above is satisfied.
- For each problem found, add a QAIssue with:
  - `file`: the exact file path affected (or "GENERAL" for cross-cutting issues)
  - `severity`: "critical" (blocks execution), "major" (wrong behaviour),
    or "minor" (cosmetic / style)
  - `description`: ONE sentence saying what is wrong AND how to fix it.
- If passed is true, the issues list must be empty.
- Be strict. Missing functionality, placeholder TODOs, or empty files = critical.
"""


def qa_node(state: AgentState) -> dict:
    """LangGraph node: run the QA agent."""

    print("\n" + "=" * 60)
    print("🔍 QA AGENT — Validating Code vs Specs")
    print("=" * 60)

    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    iteration = state.get("iteration", 0)

    # ── Hard guard: no files = immediate FAIL ──────────────────
    if not code_files:
        feedback = (
            "CRITICAL: No code files were generated at all. "
            "The Developer agent must produce at minimum:\n"
            + "\n".join(f"  - {f}" for f in REQUIRED_FILES)
        )
        issues = [
            {"file": "GENERAL", "severity": "critical", "description": "No code files were generated. Generate all mandatory files."}
        ]
        print(f"\n[PLAN] Checking code files … found 0 files.")
        print("[ACT]  IMMEDIATE FAIL — no code files in state.")
        print(f"[REASON] {feedback}\n")

        return {
            "qa_passed": False,
            "qa_feedback": feedback,
            "qa_issues": issues,
            "iteration": iteration + 1,
            "reasoning_logs": [
                {"agent": "qa", "phase": "plan", "content": "Found 0 code files in state."},
                {"agent": "qa", "phase": "act", "content": "IMMEDIATE FAIL — skipped LLM."},
                {"agent": "qa", "phase": "reason", "content": feedback},
            ],
        }

    # ── Check for missing required files (pre-LLM) ────────────
    generated_paths = {f["path"] for f in code_files}
    missing = [r for r in REQUIRED_FILES if not any(r in p for p in generated_paths)]

    pre_issues: list[dict] = []
    if missing:
        print(f"[PRE-CHECK] Missing required files: {missing}")
        for m in missing:
            pre_issues.append({
                "file": m,
                "severity": "critical",
                "description": f"Required file '{m}' is missing. Generate it.",
            })

    # ── Build readable code view for LLM ───────────────────────
    code_view = ""
    for f in code_files:
        lang = f.get("language", "python")
        code_view += f"\n### File: {f['path']}\n```{lang}\n{f['content']}\n```\n"

    # ── Plan phase ─────────────────────────────────────────────
    plan_trace = f"Reviewing {len(code_files)} code files against specifications."
    print(f"\n[PLAN] {plan_trace}")

    prefail_note = ""
    if missing:
        prefail_note = (
            "NOTE: The following required files are MISSING — this MUST result in "
            "passed=false with critical issues:\n"
            + "\n".join(f"  - {f}" for f in missing)
            + "\n\n"
        )

    user_msg = (
        f"{prefail_note}"
        f"## Original Specifications\n{specs}\n\n"
        f"## Generated Code Files ({len(code_files)} files)\n{code_view}"
    )

    # ── Act phase: structured output ───────────────────────────
    print("[ACT]  Calling LLM to validate code (structured output) …")

    llm = ChatOpenAI(
        model="deepseek-r1",
        temperature=0.1,
        max_tokens=4096,
        openai_api_key=os.getenv("SNOWFLAKE_API_KEY"),
        openai_api_base=os.getenv("SNOWFLAKE_API_BASE"),
    )

    qa_passed = False
    qa_issues: list[dict] = list(pre_issues)
    qa_feedback = ""

    try:
        structured_llm = llm.with_structured_output(QAResult)
        result: QAResult = structured_llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_msg),
        ])
        qa_passed = result.passed
        qa_issues.extend(iss.model_dump() for iss in result.issues)

        # Build human-readable feedback from issues
        if qa_issues:
            qa_feedback = "\n".join(
                f"[{iss['severity'].upper()}] {iss['file']}: {iss['description']}"
                for iss in qa_issues
            )
        else:
            qa_feedback = "All checks passed."

    except Exception as e:
        print(f"[ACT]  Structured QA failed ({type(e).__name__}: {e})")
        print("[ACT]  Falling back to raw LLM …")

        # Fallback: raw call + parse
        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_msg),
        ])
        raw = response.content
        qa_feedback = raw

        # Try to determine pass/fail from raw text
        raw_upper = raw.upper()
        if "PASS" in raw_upper and "FAIL" not in raw_upper:
            qa_passed = True
        else:
            qa_passed = False
            # Create a generic issue from the raw feedback
            qa_issues.append({
                "file": "GENERAL",
                "severity": "major",
                "description": raw[:500],
            })

    # ── Override: force FAIL if required files are missing ─────
    if missing and qa_passed:
        print("[OVERRIDE] LLM said PASS but required files are missing — forcing FAIL.")
        qa_passed = False

    # ── Override: force FAIL if pre_issues exist ───────────────
    if pre_issues and qa_passed:
        qa_passed = False

    status = "PASS ✅" if qa_passed else "FAIL ❌"

    # ── Reason phase ───────────────────────────────────────────
    issue_summary = f"{len(qa_issues)} issues" if qa_issues else "no issues"
    reason_trace = f"QA result: {status}. Files reviewed: {len(code_files)}. Found {issue_summary}."
    print(f"[REASON] {reason_trace}")

    if qa_issues:
        print("[TICKETS]")
        for iss in qa_issues:
            icon = {"critical": "🔴", "major": "🟠", "minor": "🟡"}.get(iss["severity"], "⚪")
            print(f"  {icon} [{iss['severity'].upper()}] {iss['file']}: {iss['description']}")
        print()
    else:
        print(f"[FEEDBACK] {qa_feedback[:200]}\n")

    return {
        "qa_passed": qa_passed,
        "qa_feedback": qa_feedback,
        "qa_issues": qa_issues,
        "iteration": iteration + 1,
        "reasoning_logs": [
            {"agent": "qa", "phase": "plan", "content": plan_trace},
            {"agent": "qa", "phase": "act", "content": f"Validation result: {status}"},
            {"agent": "qa", "phase": "reason", "content": reason_trace},
        ],
    }
