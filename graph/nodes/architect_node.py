"""Architect node — Master Plan + task dispatch.

Responsibilities:
  1. Parse the user request into structured specifications.
  2. Identify modules, API routes, data models, and user journeys.
  3. Generate C4 Context and Container diagrams in Mermaid syntax.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from graph.prompts.architect_prompts import ARCHITECT_SYSTEM_PROMPT
from graph.state import AgentState
from integrations.cortex import get_cortex_llm


def architect_node(state: AgentState) -> dict:
    """LangGraph node: run the Architect agent.

    Args:
        state: Current pipeline state containing ``user_request``.

    Returns:
        A dict updating ``specs``, ``diagrams``, and ``reasoning_logs``.
    """
    print("\n" + "=" * 60)
    print("📋 ARCHITECT AGENT — Planning & Master Design")
    print("=" * 60)

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.3, max_tokens=4096)
    user_request = state["user_request"]

    # ── Plan phase ──────────────────────────────────────────────
    plan_trace = (
        f"Analysing user request: '{user_request}'. "
        "Will produce specs + C4 diagrams."
    )
    print(f"\n[PLAN] {plan_trace}")

    # ── Act phase ───────────────────────────────────────────────
    print("[ACT]  Calling LLM to generate specifications and diagrams …")

    response = llm.invoke(
        [
            SystemMessage(content=ARCHITECT_SYSTEM_PROMPT),
            HumanMessage(content=f"Application to design:\n\n{user_request}"),
        ]
    )

    raw = response.content

    # ── Parse output ────────────────────────────────────────────
    specs = ""
    diagrams = ""

    if "===SPECS_START===" in raw and "===SPECS_END===" in raw:
        specs = (
            raw.split("===SPECS_START===")[1]
            .split("===SPECS_END===")[0]
            .strip()
        )
    else:
        specs = raw  # fallback: treat the entire output as specs

    if "===DIAGRAMS_START===" in raw and "===DIAGRAMS_END===" in raw:
        diagrams = (
            raw.split("===DIAGRAMS_START===")[1]
            .split("===DIAGRAMS_END===")[0]
            .strip()
        )

    # ── Reason phase ────────────────────────────────────────────
    reason_trace = f"Produced {len(specs)} chars of specs and {len(diagrams)} chars of diagrams."
    print(f"[REASON] {reason_trace}\n")

    return {
        "specs": specs,
        "diagrams": diagrams,
        "reasoning_logs": [
            {"agent": "architect", "phase": "plan", "content": plan_trace},
            {
                "agent": "architect",
                "phase": "act",
                "content": "Generated specs and C4 diagrams.",
            },
            {"agent": "architect", "phase": "reason", "content": reason_trace},
        ],
    }
