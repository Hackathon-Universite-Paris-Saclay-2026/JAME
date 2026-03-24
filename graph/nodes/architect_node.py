"""Architect node — Full AIDL-compliant design flow.

Flow:
  1. Classify scope (function / feature / system / product).
  2. Interrogation - up to 3 rounds of Q&A for system/product scope (CLI mode only).
  3. Design - full C4 (system/product) or lightweight spec (function/feature).
  4. Self-critique - automated internal consistency check.
  5. User validation loop - up to MAX_DESIGN_ITERATIONS revisions (CLI mode only).
  6. Memory compression when context exceeds threshold.

In API mode (no TTY), interrogation and user validation are skipped so the
server never blocks waiting for stdin.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from cancel_token import raise_if_cancelled
from graph.prompts.architect_prompts import (
    DESIGN_INITIAL_PROMPT,
    DESIGN_LIGHTWEIGHT_PROMPT,
    DESIGN_REVISION_PROMPT,
    INTERROGATION_ROUND_PROMPT,
    MEMORY_COMPRESS_PROMPT,
    SCOPE_CLASSIFIER_PROMPT,
    SELF_CRITIQUE_PROMPT,
    VALIDATION_PROMPT,
)
from graph.state import AgentState
from integrations.cortex import get_cortex_llm
from utils.node import maybe_compress, parse_json_safe, strip_thinking


MAX_INTERROGATION_ROUNDS = 3
MAX_DESIGN_ITERATIONS = 3
COMPRESS_THRESHOLD = 5000

MODE_PREAMBLE: dict[str, str] = {
    "junior": "Generate a simple, minimal implementation. Focus on clarity over completeness.",
    "senior": "Generate a well-structured, production-quality implementation with clear separation of concerns.",
    "expert": "Generate an enterprise-grade implementation with full observability, scalability, and security considerations.",
}


def _is_interactive() -> bool:
    """Return True when running in a terminal (CLI mode), False in API/server mode."""
    return sys.stdin.isatty()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_context(user_request: str, clarifications: list[dict]) -> str:
    lines = [f"## Application request\n{user_request}"]
    if clarifications:
        lines.append("## Clarifications")
        for qa in clarifications:
            lines.append(f"Q: {qa['question']}\nA: {qa['answer']}")
    return "\n\n".join(lines)


def _parse_output(raw: str) -> tuple[str, str]:
    specs = diagrams = ""
    if "===SPECS_START===" in raw and "===SPECS_END===" in raw:
        specs = (
            raw.split("===SPECS_START===")[1]
            .split("===SPECS_END===")[0]
            .strip()
        )
    else:
        specs = raw
    if "===DIAGRAMS_START===" in raw and "===DIAGRAMS_END===" in raw:
        diagrams = (
            raw.split("===DIAGRAMS_START===")[1]
            .split("===DIAGRAMS_END===")[0]
            .strip()
        )
    return specs, diagrams


# ── Sub-phases ────────────────────────────────────────────────────────────────


def _classify_scope(llm: BaseChatModel, user_request: str) -> tuple[str, bool]:
    """Call the LLM to classify scope and decide whether interrogation is needed."""
    print("[ACT]  Classifying request scope …")
    resp = llm.invoke(
        [
            SystemMessage(content=SCOPE_CLASSIFIER_PROMPT),
            HumanMessage(content=user_request),
        ]
    )
    _, raw = strip_thinking(resp.content)
    data = parse_json_safe(raw)
    scope = data.get("scope", "system")
    needs_interrogation = data.get(
        "needs_interrogation", scope in ("system", "product")
    )
    print(f"[ACT]  Scope: {scope} | needs_interrogation: {needs_interrogation}")
    return scope, needs_interrogation


def _interrogate(
    llm: BaseChatModel, user_request: str, max_rounds: int
) -> list[dict]:
    """Run up to max_rounds of Q&A with the user to clarify ambiguities.

    Only called in interactive (CLI) mode — skipped in API mode.
    """
    clarifications: list[dict] = []
    for round_num in range(1, max_rounds + 1):
        raise_if_cancelled()
        previous_qa = (
            "\n".join(
                f"Q: {c['question']}\nA: {c['answer']}" for c in clarifications
            )
            or "None yet."
        )
        resp = llm.invoke(
            [
                SystemMessage(
                    content=INTERROGATION_ROUND_PROMPT.format(
                        round=round_num,
                        max_rounds=max_rounds,
                        user_request=user_request,
                        previous_qa=previous_qa,
                    )
                ),
                HumanMessage(
                    content="Generate the next set of clarification questions."
                ),
            ]
        )
        _, raw = strip_thinking(resp.content)
        data = parse_json_safe(raw)
        questions: list[str] = data.get("questions", [])
        done: bool = data.get("done", False)

        if not questions or done:
            print(f"[ACT]  Interrogation complete after round {round_num}.")
            break

        print("\n" + "─" * 60)
        print(f"[CLARIFY] Round {round_num}/{max_rounds}:")
        print("─" * 60)
        for i, q in enumerate(questions, 1):
            print(f"  [{i}] {q}")
            answer = input("    ➜ ").strip() or "No preference / not applicable"
            clarifications.append({"question": q, "answer": answer})
            print()
        print("─" * 60)

        if done:
            break

    return clarifications


def _interrogate_api(
    llm: BaseChatModel,
    user_request: str,
    max_rounds: int,
    callback: Callable[[str, list[str]], str],
) -> list[dict]:
    """Run up to max_rounds of Q&A using the API callback instead of stdin.

    Called in API mode when a clarification_callback is present in state.
    """
    clarifications: list[dict] = []
    for round_num in range(1, max_rounds + 1):
        raise_if_cancelled()
        previous_qa = (
            "\n".join(
                f"Q: {c['question']}\nA: {c['answer']}" for c in clarifications
            )
            or "None yet."
        )
        resp = llm.invoke(
            [
                SystemMessage(
                    content=INTERROGATION_ROUND_PROMPT.format(
                        round=round_num,
                        max_rounds=max_rounds,
                        user_request=user_request,
                        previous_qa=previous_qa,
                    )
                ),
                HumanMessage(
                    content="Generate the next set of clarification questions."
                ),
            ]
        )
        _, raw = strip_thinking(resp.content)
        data = parse_json_safe(raw)
        questions: list[str] = data.get("questions", [])
        done: bool = data.get("done", False)

        if not questions or done:
            break

        for q in questions:
            answer = callback(q, []) or "No preference / not applicable"
            if not answer.strip():
                answer = "No preference / not applicable"
            clarifications.append({"question": q, "answer": answer})

        if done:
            break

    return clarifications


def _self_critique(llm: BaseChatModel, specs: str, diagrams: str) -> list[str]:
    """Ask the LLM to self-review the design for inconsistencies."""
    print("[ACT]  Running self-critique …")
    resp = llm.invoke(
        [
            HumanMessage(
                content=SELF_CRITIQUE_PROMPT.format(
                    specs=specs, diagrams=diagrams
                )
            ),
        ]
    )
    _, raw = strip_thinking(resp.content)
    data = parse_json_safe(raw)
    issues: list[str] = data.get("issues", [])
    if issues:
        print(f"[REASON] Self-critique found {len(issues)} issue(s): {issues}")
    else:
        print("[REASON] Self-critique passed — design is coherent.")
    return issues


def _validate_with_user(
    llm: BaseChatModel, specs: str, diagrams: str
) -> tuple[bool, str | None]:
    """Present the design summary to the user and collect approval or feedback.

    Only called in interactive (CLI) mode — skipped in API mode.
    """
    print("\n" + "=" * 60)
    print("[VALIDATE] Presenting design for approval …")
    print("=" * 60)
    resp = llm.invoke(
        [
            SystemMessage(content=VALIDATION_PROMPT.format(specs=specs)),
            HumanMessage(content=f"## Diagrams\n{diagrams[:1000]}"),
        ]
    )
    _, review = strip_thinking(resp.content)
    print("\n" + review)
    answer = input("\n    ➜ ").strip()
    print()
    if answer.lower() in ("y", "yes", "oui", "ok", "approve", "approved", ""):
        print("[VALIDATE] ✅ Approved.\n")
        return True, None
    print("[VALIDATE] 📝 Feedback received — revising …\n")
    return False, answer


# ── Main node ─────────────────────────────────────────────────────────────────


def architect_node(state: AgentState) -> dict:
    """LangGraph node: run the full Architect agent flow.

    Args:
        state: Current pipeline state containing ``user_request``.

    Returns:
        A dict updating ``specs``, ``diagrams``, and ``reasoning_logs``.
    """
    raise_if_cancelled()
    print("\n" + "=" * 60)
    print("🏛️  ARCHITECT AGENT — Full Design Flow")
    print("=" * 60)

    interactive = _is_interactive()
    llm = get_cortex_llm(model="deepseek-r1", temperature=0.3, max_tokens=4096)
    user_request = state["user_request"]
    memory = ""
    reasoning_logs: list[dict] = []
    _emit = state.get("emit_callback")

    def _log(entry: dict) -> None:
        reasoning_logs.append(entry)
        if _emit:
            _emit(entry)

    print(f"\n[PLAN] Analysing: '{user_request}'")
    _log(
        {
            "agent": "architect",
            "phase": "plan",
            "content": f"Analysing: '{user_request}'",
        }
    )

    # ── Step 1: Classify scope ───────────────────────────────────────────────
    scope, needs_interrogation = _classify_scope(llm, user_request)
    _log(
        {
            "agent": "architect",
            "phase": "classify",
            "content": f"Scope: {scope}, needs_interrogation: {needs_interrogation}",
        }
    )

    # ── Step 2: Interrogation ────────────────────────────────────────────────
    clarifications: list[dict] = []
    callback = state.get("clarification_callback")
    if needs_interrogation and callback:
        clarifications = _interrogate_api(
            llm, user_request, MAX_INTERROGATION_ROUNDS, callback
        )
        _log(
            {
                "agent": "architect",
                "phase": "interrogation",
                "content": f"{len(clarifications)} clarification(s) collected via API.",
            }
        )
    elif needs_interrogation and interactive:
        clarifications = _interrogate(
            llm, user_request, MAX_INTERROGATION_ROUNDS
        )
        _log(
            {
                "agent": "architect",
                "phase": "interrogation",
                "content": f"{len(clarifications)} clarification(s) collected.",
            }
        )
    elif needs_interrogation:
        _log(
            {
                "agent": "architect",
                "phase": "interrogation",
                "content": "API mode — skipping interactive interrogation.",
            }
        )

    # ── Step 3: Build context + compress if needed ───────────────────────────
    mode = state.get("mode", "senior")
    mode_note = MODE_PREAMBLE.get(mode, MODE_PREAMBLE["senior"])
    context = _build_context(user_request, clarifications)
    if mode_note:
        context = f"## Mode instruction\n{mode_note}\n\n{context}"
    memory = maybe_compress(llm, context, memory, MEMORY_COMPRESS_PROMPT)
    memory_section = (
        f"## Memory / previous context\n{memory}\n\n" if memory else ""
    )

    # ── Step 4: Design + self-critique + user validation loop ────────────────
    specs = diagrams = ""
    approved = False
    feedback = ""

    for iteration in range(1, MAX_DESIGN_ITERATIONS + 1):
        raise_if_cancelled()
        print(f"[ACT]  Design iteration {iteration}/{MAX_DESIGN_ITERATIONS} …")

        if scope in ("function", "feature"):
            messages = [
                SystemMessage(
                    content=DESIGN_LIGHTWEIGHT_PROMPT.format(scope=scope)
                ),
                HumanMessage(content=context),
            ]
        elif iteration == 1:
            messages = [
                SystemMessage(
                    content=DESIGN_INITIAL_PROMPT.format(
                        memory_section=memory_section
                    )
                ),
                HumanMessage(content=context),
            ]
        else:
            messages = [
                SystemMessage(
                    content=DESIGN_REVISION_PROMPT.format(
                        memory_section=memory_section, feedback=feedback
                    )
                ),
                HumanMessage(content=context),
            ]

        d_resp = llm.invoke(messages)
        thinking, raw = strip_thinking(d_resp.content)
        if thinking:
            print(
                f"[THINKING — design {iteration}]\n"
                + thinking[:500]
                + "\n"
                + "─" * 60
            )

        specs, diagrams = _parse_output(raw)
        _log(
            {
                "agent": "architect",
                "phase": "act",
                "thinking": thinking,
                "content": f"Iteration {iteration}: {len(specs)}c specs, {len(diagrams)}c diagrams.",
            }
        )

        # Self-critique
        if scope not in ("function", "feature"):
            critique_issues = _self_critique(llm, specs, diagrams)
            reasoning_logs.append(
                {
                    "agent": "architect",
                    "phase": "self_critique",
                    "content": f"Issues: {critique_issues}"
                    if critique_issues
                    else "Passed.",
                }
            )

        # Lightweight scope skips user validation
        if scope in ("function", "feature"):
            approved = True
            break

        # API mode: request specs approval for system/product scopes when
        # a clarification_callback is available (i.e. a WebSocket client is
        # connected).  Simple scopes auto-approve as before.
        if not interactive:
            _specs_review_callback = state.get("clarification_callback")
            _mode = state.get("mode", "senior")
            _needs_review = (
                scope in ("system", "product")
                and _mode in ("senior",)
                and _specs_review_callback is not None
            )
            if _needs_review:
                _log(
                    {
                        "agent": "architect",
                        "phase": "specs_review",
                        "content": "Waiting for specs approval before developer starts.",
                    }
                )
                # Emit a special specs_review_request event — reuse the
                # clarification_callback so we don't need a new callback type.
                # The payload carries the full specs so the UI can render them.
                _review_question = (
                    "Please review the generated specifications. "
                    "Reply 'approve' (or leave blank) to proceed, "
                    "or describe changes you want."
                )
                answer = _specs_review_callback(
                    _review_question,
                    ["approve | Proceed to code generation as-is"],
                )
                answer = (answer or "").strip()
                if answer.lower() in (
                    "",
                    "approve",
                    "approved",
                    "ok",
                    "y",
                    "yes",
                ):
                    approved = True
                    _log(
                        {
                            "agent": "architect",
                            "phase": "specs_review",
                            "content": "Specs approved by user.",
                        }
                    )
                    break
                # Treat answer as revision feedback
                feedback = answer
                _log(
                    {
                        "agent": "architect",
                        "phase": "specs_review",
                        "content": f"Revision requested: {feedback}",
                    }
                )
                memory = maybe_compress(
                    llm,
                    f"Revision feedback: {feedback}",
                    memory,
                    MEMORY_COMPRESS_PROMPT,
                )
                memory_section = f"## Memory / previous context\n{memory}\n\n"
                continue
            approved = True
            _log(
                {
                    "agent": "architect",
                    "phase": "validate",
                    "content": "API mode — auto-approved design.",
                }
            )
            break

        approved, feedback = _validate_with_user(llm, specs, diagrams)
        if approved:
            break

        memory = maybe_compress(
            llm,
            f"Iteration {iteration} feedback: {feedback}",
            memory,
            MEMORY_COMPRESS_PROMPT,
        )
        memory_section = f"## Memory / previous context\n{memory}\n\n"

    if not approved:
        print("[VALIDATE] ⚠️  Max iterations reached — using last design.\n")

    reason_trace = f"Final: {len(specs)}c specs, {len(diagrams)}c diagrams."
    print(f"[REASON] {reason_trace}\n")
    _log({"agent": "architect", "phase": "reason", "content": reason_trace})

    run_output_dir = state.get("run_output_dir", "")
    if run_output_dir:
        output_dir = (Path(run_output_dir) / "output").resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if specs:
            (output_dir / "specifications.md").write_text(
                specs, encoding="utf-8"
            )
        if diagrams:
            (output_dir / "c4_diagrams.md").write_text(
                diagrams, encoding="utf-8"
            )

    run_output_dir = state.get("run_output_dir", "")
    if run_output_dir:
        output_dir = (Path(run_output_dir) / "output").resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if specs:
            (output_dir / "specifications.md").write_text(
                specs, encoding="utf-8"
            )
        if diagrams:
            (output_dir / "c4_diagrams.md").write_text(
                diagrams, encoding="utf-8"
            )

    return {
        "scope": scope,
        "specs": specs,
        "diagrams": diagrams,
        "specs_approved": approved,
        "specs_revision_feedback": feedback or "",
        "awaiting_specs_approval": False,
        "reasoning_logs": reasoning_logs,
    }
