"""QA node — Validates generated code against specifications.

Returns structured "actionable tickets" (list[QAIssue]) instead of
free-form text so the Developer node can address issues precisely.
Routes failures back to the Developer node via the graph's conditional edge.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from graph.prompts.qa_prompts import QA_SYSTEM_PROMPT
from graph.state import AgentState, QAResult
from integrations.cortex import get_cortex_llm


# Minimum files the Developer MUST produce.
REQUIRED_FILES = [
    "backend/main.py",
    "backend/models.py",
    "tests/test_main.py",
    "frontend/src/App.js",
]


def qa_node(state: AgentState) -> dict:
    """LangGraph node: run the QA agent.

    Validates all generated code files against the original specifications
    using structured output (QAResult). Falls back to raw LLM + heuristic
    parsing if structured output fails.

    Args:
        state: Current pipeline state with ``specs``, ``code_files``, and
               ``iteration``.

    Returns:
        A dict updating ``qa_passed``, ``qa_feedback``, ``qa_issues``,
        ``iteration``, and ``reasoning_logs``.
    """
    print("\n" + "=" * 60)
    print("🔍 QA AGENT — Validating Code vs Specs")
    print("=" * 60)

    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    iteration = state.get("iteration", 0)

    # ── Hard guard: no files → immediate FAIL ──────────────────
    if not code_files:
        feedback = (
            "CRITICAL: No code files were generated at all. "
            "The Developer agent must produce at minimum:\n"
            + "\n".join(f"  - {f}" for f in REQUIRED_FILES)
        )
        issues = [
            {
                "file": "GENERAL",
                "severity": "critical",
                "description": "No code files were generated. Generate all mandatory files.",
            }
        ]
        print("\n[PLAN] Checking code files … found 0 files.")
        print("[ACT]  IMMEDIATE FAIL — no code files in state.")
        print(f"[REASON] {feedback}\n")

        return {
            "qa_passed": False,
            "qa_feedback": feedback,
            "qa_issues": issues,
            "iteration": iteration + 1,
            "reasoning_logs": [
                {
                    "agent": "qa",
                    "phase": "plan",
                    "content": "Found 0 code files in state.",
                },
                {
                    "agent": "qa",
                    "phase": "act",
                    "content": "IMMEDIATE FAIL — skipped LLM.",
                },
                {"agent": "qa", "phase": "reason", "content": feedback},
            ],
        }

    # ── Pre-LLM: check for missing required files ───────────────
    generated_paths = {f["path"] for f in code_files}
    missing = [
        r for r in REQUIRED_FILES if not any(r in p for p in generated_paths)
    ]

    pre_issues: list[dict] = []
    if missing:
        print(f"[PRE-CHECK] Missing required files: {missing}")
        for m in missing:
            pre_issues.append(
                {
                    "file": m,
                    "severity": "critical",
                    "description": f"Required file '{m}' is missing. Generate it.",
                }
            )

    # ── Build readable code view for the LLM ───────────────────
    code_view = ""
    for f in code_files:
        lang = f.get("language", "python")
        code_view += (
            f"\n### File: {f['path']}\n```{lang}\n{f['content']}\n```\n"
        )

    # ── Plan phase ─────────────────────────────────────────────
    plan_trace = (
        f"Reviewing {len(code_files)} code files against specifications."
    )
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

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.1, max_tokens=4096)

    qa_passed = False
    qa_issues: list[dict] = list(pre_issues)
    qa_feedback = ""

    try:
        structured_llm = llm.with_structured_output(QAResult)
        result: QAResult = structured_llm.invoke(
            [
                SystemMessage(content=QA_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ]
        )
        qa_passed = result.passed
        qa_issues.extend(iss.model_dump() for iss in result.issues)

        qa_feedback = (
            "\n".join(
                f"[{iss['severity'].upper()}] {iss['file']}: {iss['description']}"
                for iss in qa_issues
            )
            if qa_issues
            else "All checks passed."
        )

    except Exception as e:
        print(f"[ACT]  Structured QA failed ({type(e).__name__}: {e})")
        print("[ACT]  Falling back to raw LLM …")

        response = llm.invoke(
            [
                SystemMessage(content=QA_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ]
        )
        raw = response.content
        qa_feedback = raw

        raw_upper = raw.upper()
        if "PASS" in raw_upper and "FAIL" not in raw_upper:
            qa_passed = True
        else:
            qa_passed = False
            qa_issues.append(
                {
                    "file": "GENERAL",
                    "severity": "major",
                    "description": raw[:500],
                }
            )

    # ── Override: force FAIL if required files are missing ─────
    if missing and qa_passed:
        print(
            "[OVERRIDE] LLM said PASS but required files are missing — forcing FAIL."
        )
        qa_passed = False

    if pre_issues and qa_passed:
        qa_passed = False

    # ── Reason phase ────────────────────────────────────────────
    reason_trace = (
        f"QA {'PASSED' if qa_passed else 'FAILED'} — "
        f"{len(qa_issues)} issue(s) found."
    )
    print(f"[REASON] {reason_trace}\n")

    return {
        "qa_passed": qa_passed,
        "qa_feedback": qa_feedback,
        "qa_issues": qa_issues,
        "iteration": iteration + 1,
        "reasoning_logs": [
            {"agent": "qa", "phase": "plan", "content": plan_trace},
            {
                "agent": "qa",
                "phase": "act",
                "content": f"Validated {len(code_files)} files.",
            },
            {"agent": "qa", "phase": "reason", "content": reason_trace},
        ],
    }
