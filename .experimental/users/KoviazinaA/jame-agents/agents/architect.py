"""Architect Agent — AIDL-compliant

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

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from state import AgentState

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

MAX_DESIGN_ITERATIONS = 3
COMPRESS_THRESHOLD    = 5000
PROMPTS_DIR           = Path(__file__).parent.parent / "prompts"

# ──────────────────────────────────────────────────────────────────────────────
# Prompts (loaded from prompts/)
# ──────────────────────────────────────────────────────────────────────────────

def _load(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")

INTERROGATION_PROMPT = _load("interrogation.md")
DESIGN_PROMPT        = _load("design.md")
VALIDATION_PROMPT    = _load("validation.md")
COMPRESS_PROMPT      = _load("compress.md")
REVISION_PROMPT      = _load("revision.md")

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


def _parse_questions(raw: str) -> list[str]:
    try:
        qs = json.loads(raw)
        return qs if isinstance(qs, list) else []
    except json.JSONDecodeError:
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return []


def _ask_questions(questions: list[str]) -> list[dict]:
    if not questions:
        return []
    print("\n" + "─" * 60)
    print("[CLARIFY] Questions to refine the design:")
    print("─" * 60)
    pairs = []
    for i, q in enumerate(questions, 1):
        print(f"  [{i}/{len(questions)}] {q}")
        answer = input("    ➜ ").strip()
        pairs.append({"question": q, "answer": answer or "No preference / not applicable"})
        print()
    print("─" * 60)
    return pairs


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


def _validate(llm: ChatOpenAI, specs: str, diagrams: str) -> tuple[bool, Optional[str]]:
    print("\n" + "=" * 60)
    print("[VALIDATE] Presenting design for approval …")
    print("=" * 60)
    resp = llm.invoke([
        SystemMessage(content=VALIDATION_PROMPT),
        HumanMessage(content=f"## Specs\n{specs[:2000]}\n\n## Diagrams\n{diagrams[:500]}"),
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

    # [LOG] PLAN
    print(f"\n[PLAN] Analysing: '{user_request}'")
    reasoning_logs.append({"agent": "architect", "phase": "plan",
                            "content": f"Analysing: '{user_request}'"})

    # [ANALYSE] Interrogation
    print("[ACT]  Generating clarification questions …")
    q_resp = llm.invoke([
        SystemMessage(content=INTERROGATION_PROMPT),
        HumanMessage(content=f"Application:\n{user_request}"),
    ])
    thinking, q_raw = _strip_thinking(q_resp.content)
    if thinking:
        print("[THINKING — interrogation]\n" + thinking + "\n" + "─" * 60)

    questions = _parse_questions(q_raw)
    reasoning_logs.append({"agent": "architect", "phase": "clarify",
                            "content": f"{len(questions)} clarification questions generated."})

    # [VALIDATE] Q&A with user
    clarifications = _ask_questions(questions)

    # [MEMORY] Build & compress context
    context = _build_context(user_request, clarifications)
    memory  = _maybe_compress(llm, context, memory)
    memory_section = f"## Memory / previous context\n{memory}\n" if memory else ""

    # [ACT] Design iteration loop
    specs = diagrams = ""
    approved = False
    feedback = ""

    for iteration in range(1, MAX_DESIGN_ITERATIONS + 1):
        print(f"[ACT]  Design iteration {iteration}/{MAX_DESIGN_ITERATIONS} …")

        if iteration == 1:
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

        # [VALIDATE] User approval
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
