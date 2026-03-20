"""Architect Agent

Uses the same AgentState / return keys (specs, diagrams, reasoning_logs).

AIDL rules:
  [LOG]      [PLAN] [ACT] [REASON] [THINKING]
  [ANALYSE]  Parse and understand input specifications
  [MODULES]  Identify modules, components, constraints
  [JOURNEY]  Map user journeys per actor
  [VALIDATE] Present specs/plan to user and loop until approved
  [MEMORY]   Maintain conversation context; compress when too large
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from state import AgentState

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

MAX_DESIGN_ITERATIONS = 3
COMPRESS_THRESHOLD    = 5000
PROMPTS_FILE          = Path(__file__).parent.parent / "prompts" / "solution_architect.yaml"

# ──────────────────────────────────────────────────────────────────────────────
# Prompts (loaded from prompts/solution_architect.yaml)
# ──────────────────────────────────────────────────────────────────────────────

def _load_prompts() -> dict:
    with open(PROMPTS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)

_PROMPTS = _load_prompts()

SCOPE_CLASSIFIER_PROMPT    = _PROMPTS["analysis"]["scope_classifier"]
INTERROGATION_ROUND_PROMPT = _PROMPTS["analysis"]["interrogation_round"]
DESIGN_PROMPT              = _PROMPTS["design"]["initial"]
DESIGN_LIGHTWEIGHT_PROMPT  = _PROMPTS["design"]["lightweight"]
REVISION_PROMPT            = _PROMPTS["design"]["revision"]
SELF_CRITIQUE_PROMPT       = _PROMPTS["quality"]["self_critique"]
VALIDATION_PROMPT          = _PROMPTS["interaction"]["validation"]
COMPRESS_PROMPT            = _PROMPTS["memory"]["compress"]

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model="deepseek-r1",
        temperature=0.3,
        max_tokens=4096,
        openai_api_key=os.environ["SNOWFLAKE_API_KEY"],
        base_url=os.environ["SNOWFLAKE_API_BASE"],
    )


def _strip_thinking(text: str) -> tuple[str, str]:
    if "<think>" in text and "</think>" in text:
        thinking = text.split("<think>")[1].split("</think>")[0].strip()
        content  = text.split("</think>", 1)[-1].strip()
        return thinking, content
    return "", text


MAX_INTERROGATION_ROUNDS = 3

def _parse_json_response(raw: str, fallback: dict) -> dict:
    """Extract JSON from LLM response, stripping markdown fences if needed."""
    raw = raw.strip()
    # strip ```json ... ``` fences
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return fallback


def _classify_scope(llm: ChatOpenAI, user_request: str) -> tuple[str, bool]:
    """Return (scope, needs_interrogation)."""
    resp = llm.invoke([
        SystemMessage(content=SCOPE_CLASSIFIER_PROMPT),
        HumanMessage(content=user_request),
    ])
    _, raw = _strip_thinking(resp.content)
    result = _parse_json_response(raw, {"scope": "system", "needs_interrogation": True})
    scope = result.get("scope", "system")
    needs = result.get("needs_interrogation", True)
    print(f"[PLAN] Scope detected: {scope} | Interrogation needed: {needs}")
    return scope, needs


def _ask_questions(questions: list[str], round_num: int, max_rounds: int) -> list[dict]:
    if not questions:
        return []
    print("\n" + "─" * 60)
    print(f"[CLARIFY] Round {round_num}/{max_rounds} — {len(questions)} question(s):")
    print("─" * 60)
    pairs = []
    for i, q in enumerate(questions, 1):
        print(f"  [{i}] {q}")
        answer = input("    ➜ ").strip()
        pairs.append({"question": q, "answer": answer or "No preference / not applicable"})
        print()
    print("─" * 60)
    return pairs


def _interrogation_loop(llm: ChatOpenAI, user_request: str) -> list[dict]:
    """Run up to MAX_INTERROGATION_ROUNDS rounds of 4 questions each."""
    all_qa: list[dict] = []

    for round_num in range(1, MAX_INTERROGATION_ROUNDS + 1):
        print(f"[ACT]  Interrogation round {round_num}/{MAX_INTERROGATION_ROUNDS} …")

        previous_qa_text = "\n".join(
            f"Q: {qa['question']}\nA: {qa['answer']}" for qa in all_qa
        ) if all_qa else "None yet."

        prompt = INTERROGATION_ROUND_PROMPT.format(
            round=round_num,
            max_rounds=MAX_INTERROGATION_ROUNDS,
            user_request=user_request,
            previous_qa=previous_qa_text,
        )
        resp = llm.invoke([HumanMessage(content=prompt)])
        thinking, raw = _strip_thinking(resp.content)
        if thinking:
            print(f"[THINKING — interrogation r{round_num}]\n" + thinking + "\n" + "─" * 60)

        result = _parse_json_response(raw, {"questions": [], "done": True})
        questions = result.get("questions", [])[:4]
        done = result.get("done", False)

        round_qa = _ask_questions(questions, round_num, MAX_INTERROGATION_ROUNDS)
        all_qa.extend(round_qa)

        if done or not questions:
            print(f"[CLARIFY] ✅ Interrogation complete after {round_num} round(s).\n")
            break

    return all_qa


def _build_context(user_request: str, clarifications: list[dict]) -> str:
    lines = [f"## Application request\n{user_request}"]
    if clarifications:
        lines.append("## Clarifications")
        for qa in clarifications:
            lines.append(f"Q: {qa['question']}\nA: {qa['answer']}")
    return "\n\n".join(lines)


def _compress(llm: ChatOpenAI, context: str) -> str:
    print("[MEMORY] Context too large — compressing …")
    resp = llm.invoke([HumanMessage(content=COMPRESS_PROMPT.format(context=context))])
    _, compressed = _strip_thinking(resp.content)
    print("[MEMORY] Compressed.\n")
    return compressed


def _maybe_compress(llm: ChatOpenAI, context: str, summary: str) -> str:
    full = (summary + "\n\n" + context).strip()
    return _compress(llm, full) if len(full) > COMPRESS_THRESHOLD else full


def _parse_output(raw: str) -> tuple[str, str]:
    specs = diagrams = ""
    if "===SPECS_START===" in raw and "===SPECS_END===" in raw:
        specs = raw.split("===SPECS_START===")[1].split("===SPECS_END===")[0].strip()
    else:
        specs = raw
    if "===DIAGRAMS_START===" in raw and "===DIAGRAMS_END===" in raw:
        diagrams = raw.split("===DIAGRAMS_START===")[1].split("===DIAGRAMS_END===")[0].strip()
    return specs, diagrams


def _self_critique(llm: ChatOpenAI, specs: str, diagrams: str) -> list[str]:
    """Check design coherence. Returns list of issues found (empty = all good)."""
    print("[QUALITY] Running self-critique …")
    resp = llm.invoke([
        HumanMessage(content=SELF_CRITIQUE_PROMPT.format(
            specs=specs[:3000], diagrams=diagrams[:1000]
        ))
    ])
    _, raw = _strip_thinking(resp.content)
    result = _parse_json_response(raw, {"issues": [], "has_issues": False})
    issues = result.get("issues", [])
    if issues:
        print(f"[QUALITY] ⚠️  {len(issues)} issue(s) found:")
        for issue in issues:
            print(f"    • {issue}")
        print()
    else:
        print("[QUALITY] ✅ Design is coherent.\n")
    return issues


def _validate(llm: ChatOpenAI, specs: str, diagrams: str) -> tuple[bool, Optional[str]]:
    print("\n" + "=" * 60)
    print("[VALIDATE] Presenting design for approval …")
    print("=" * 60)
    resp = llm.invoke([
        HumanMessage(content=VALIDATION_PROMPT.format(specs=specs)),
    ])
    _, review = _strip_thinking(resp.content)
    print("\n" + review)
    answer = input("\n    ➜ ").strip()
    print()
    if answer.lower() in ("y", "yes", "oui", "ok", "approve", "approved", ""):
        print("[VALIDATE] ✅ Approved.\n")
        return True, None
    print("[VALIDATE] 📝 Feedback received — revising …\n")
    return False, answer

# ──────────────────────────────────────────────────────────────────────────────
# LangGraph node  (same signature as kid-emmanuelle's architect_node)
# ──────────────────────────────────────────────────────────────────────────────

def architect_node(state: AgentState) -> dict:
    """AIDL-compliant architect node — drop-in for kid-emmanuelle's pipeline."""

    print("\n" + "=" * 60)
    print("🏛️  ARCHITECT AGENT")
    print("=" * 60)

    llm            = _get_llm()
    user_request   = state["user_request"]
    memory         = ""
    reasoning_logs = []

    # [PLAN] Classify scope
    print(f"\n[PLAN] Analysing: '{user_request}'")
    reasoning_logs.append({"agent": "architect", "phase": "plan",
                            "content": f"Analysing: '{user_request}'"})

    scope, needs_interrogation = _classify_scope(llm, user_request)
    reasoning_logs.append({"agent": "architect", "phase": "classify",
                            "content": f"scope={scope}, interrogation={needs_interrogation}"})

    # [ANALYSE] Iterative interrogation (system/product only)
    clarifications: list[dict] = []
    if needs_interrogation:
        clarifications = _interrogation_loop(llm, user_request)
        reasoning_logs.append({"agent": "architect", "phase": "clarify",
                                "content": f"{len(clarifications)} clarification(s) collected."})

    # [MEMORY] Build & compress context
    context = _build_context(user_request, clarifications)
    memory  = _maybe_compress(llm, context, memory)
    memory_section = f"## Memory / previous context\n{memory}\n" if memory else ""

    # [ACT] Design iteration loop
    specs = diagrams = ""
    approved = False
    feedback = ""
    is_lightweight = scope in ("function", "feature")

    for iteration in range(1, MAX_DESIGN_ITERATIONS + 1):
        print(f"[ACT]  Design iteration {iteration}/{MAX_DESIGN_ITERATIONS} …")

        if iteration == 1:
            if is_lightweight:
                messages = [
                    SystemMessage(content=DESIGN_LIGHTWEIGHT_PROMPT.format(scope=scope)),
                    HumanMessage(content=context),
                ]
            else:
                messages = [
                    SystemMessage(content=DESIGN_PROMPT.format(memory_section=memory_section)),
                    HumanMessage(content=context),
                ]
        else:
            messages = [
                SystemMessage(content=REVISION_PROMPT.format(
                    memory_section=memory_section, feedback=feedback)),
                HumanMessage(content=context),
            ]

        d_resp = llm.invoke(messages)
        thinking, raw = _strip_thinking(d_resp.content)
        if thinking:
            print(f"\n[THINKING — design {iteration}]\n" + thinking + "\n" + "─" * 60)

        specs, diagrams = _parse_output(raw)

        reasoning_logs.append({"agent": "architect", "phase": "act",
                                "content": f"Iteration {iteration}: {len(specs)}c specs, {len(diagrams)}c diagrams."})

        # [VALIDATE] User approval — only for system/product
        if is_lightweight:
            approved = True
            break

        # [QUALITY] Self-critique — let user decide whether to fix
        issues = _self_critique(llm, specs, diagrams)
        reasoning_logs.append({"agent": "architect", "phase": "quality",
                                "content": f"Self-critique: {len(issues)} issue(s).",
                                "issues": issues})

        if issues and iteration < MAX_DESIGN_ITERATIONS:
            print("[QUALITY] Fix these issues before validation? (y = fix / n = continue anyway)")
            fix_answer = input("    ➜ ").strip().lower()
            if fix_answer in ("y", "yes", "oui", "o"):
                feedback = "Fix the following issues:\n" + "\n".join(f"- {i}" for i in issues)
                memory = _maybe_compress(llm, f"Quality issues: {feedback}", memory)
                memory_section = f"## Memory / previous context\n{memory}\n"
                continue

        approved, feedback = _validate(llm, specs, diagrams)
        if approved:
            break

        # [MEMORY] Compress feedback round
        memory = _maybe_compress(llm, f"Iteration {iteration} feedback: {feedback}", memory)
        memory_section = f"## Memory / previous context\n{memory}\n"

    if not approved:
        print("[VALIDATE] ⚠️  Max iterations reached. Using last design.\n")

    # [LOG] REASON
    reason_trace = f"Final: {len(specs)}c specs, {len(diagrams)}c diagrams."
    print(f"[REASON] {reason_trace}\n")
    reasoning_logs.append({"agent": "architect", "phase": "reason", "content": reason_trace})

    return {
        "specs":          specs,
        "diagrams":       diagrams,
        "reasoning_logs": reasoning_logs,
    }
