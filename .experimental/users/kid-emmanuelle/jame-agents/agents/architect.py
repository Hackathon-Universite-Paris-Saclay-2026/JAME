"""Architect Agent — Analyses specs and produces C4 Mermaid diagrams.

Responsibilities:
  1. Parse the user request into structured specifications.
  2. Identify modules, API routes, data models, and user journeys.
  3. Generate C4 Context and Container diagrams in Mermaid syntax.
"""

from __future__ import annotations

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_groq import ChatGroq

from state import AgentState

SYSTEM_PROMPT = """\
You are the **Architect Agent** of a multi-agent software factory.

## Role
You receive a high-level application description from the user and produce:
1. **Structured Specifications** — a clear breakdown of:
   - Application modules / services
   - API routes (method, path, description)
   - Data models (name, fields, types)
   - User journeys (step-by-step flows)
   - Technical constraints and non-functional requirements
2. **C4 Diagrams in Mermaid** — generate BOTH:
   - A **Context diagram** showing external actors and the system boundary.
   - A **Container diagram** showing internal containers (API, DB, frontend, etc.).

## Output format
Return your answer in EXACTLY this structure (keep the markers):

===SPECS_START===
<your structured specifications here>
===SPECS_END===

===DIAGRAMS_START===
<your Mermaid diagrams here, each in a ```mermaid code block>
===DIAGRAMS_END===

## Rules
- Be precise and exhaustive in the specifications.
- Use standard Mermaid C4 syntax (C4Context, C4Container).
- Include relationships and labels on every arrow.
- Do NOT generate any code — only specs and diagrams.
"""


def architect_node(state: AgentState) -> dict:
    """LangGraph node: run the Architect agent."""

    print("\n" + "=" * 60)
    print("🏛️  ARCHITECT AGENT — Planning & Designing")
    print("=" * 60)

    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0.3,
        max_tokens=4096,
    )

    user_request = state["user_request"]

    # ── Plan phase ──────────────────────────────────────────────
    plan_trace = f"Analysing user request: '{user_request}'. Will produce specs + C4 diagrams."
    print(f"\n[PLAN] {plan_trace}")

    # ── Act phase ───────────────────────────────────────────────
    print("[ACT]  Calling LLM to generate specifications and diagrams …")

    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"Application to design:\n\n{user_request}"),
    ])

    raw = response.content

    # ── Parse output ────────────────────────────────────────────
    specs = ""
    diagrams = ""

    if "===SPECS_START===" in raw and "===SPECS_END===" in raw:
        specs = raw.split("===SPECS_START===")[1].split("===SPECS_END===")[0].strip()
    else:
        specs = raw  # fallback: treat entire output as specs

    if "===DIAGRAMS_START===" in raw and "===DIAGRAMS_END===" in raw:
        diagrams = raw.split("===DIAGRAMS_START===")[1].split("===DIAGRAMS_END===")[0].strip()

    # ── Reason phase ────────────────────────────────────────────
    reason_trace = (
        f"Produced {len(specs)} chars of specs and "
        f"{len(diagrams)} chars of diagrams."
    )
    print(f"[REASON] {reason_trace}\n")

    return {
        "specs": specs,
        "diagrams": diagrams,
        "reasoning_logs": [
            {"agent": "architect", "phase": "plan", "content": plan_trace},
            {"agent": "architect", "phase": "act", "content": "Generated specs and C4 diagrams."},
            {"agent": "architect", "phase": "reason", "content": reason_trace},
        ],
    }
