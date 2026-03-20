"""LangGraph workflow — AI-DLC CONSTRUCTION phase pipeline.

AI-DLC flow:
  INCEPTION
    user_request → Architect (requirements + application design)
  CONSTRUCTION
    Architect → Developer (code generation)
              → Quality Engineer (build and test) ─┐
                        ↑                           │ FAIL + iterations left
                        └───────────────────────────┘
                                    │ PASS (or max iterations reached)
                                    ↓
                                  END

The QA → Developer loop is capped at `max_iterations` to prevent infinite loops.
On PASS (or exhausted iterations), the pipeline terminates and artifacts are saved
by main.py.
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END

from state import AgentState
from agents.quality_engineer import quality_engineer_node


# ── Stub nodes (placeholders until full agents are wired in) ──────────────────

def architect_node(state: AgentState) -> dict:
    """Stub: replace with the real Architect agent when available."""
    print("\n[ARCHITECT] Stub — passing specs from state as-is.")
    return {}


def developer_node(state: AgentState) -> dict:
    """Stub: replace with the real Developer agent when available."""
    print("\n[DEVELOPER] Stub — passing code_files from state as-is.")
    return {}


# ── Conditional routing after QA ─────────────────────────────────────────────

def _route_after_qa(state: AgentState) -> str:
    """AI-DLC routing: loop back to Developer or exit."""
    if state.get("qa_passed"):
        print("\n✅  AI-DLC QA decision: PASS — pipeline complete.")
        return "end"

    iteration      = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 3)

    if iteration >= max_iterations:
        print(f"\n⚠️   AI-DLC QA decision: FAIL — max iterations ({max_iterations}) reached. "
              "Exiting with outstanding issues.")
        return "end"

    print(f"\n🔄  AI-DLC QA decision: FAIL — routing to Developer "
          f"(iteration {iteration}/{max_iterations}).")
    return "developer"


# ── Graph construction ────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """Build and compile the AI-DLC multi-agent LangGraph workflow."""

    workflow = StateGraph(AgentState)

    workflow.add_node("architect",         architect_node)
    workflow.add_node("developer",         developer_node)
    workflow.add_node("quality_engineer",  quality_engineer_node)

    workflow.set_entry_point("architect")
    workflow.add_edge("architect",        "developer")
    workflow.add_edge("developer",        "quality_engineer")

    workflow.add_conditional_edges(
        "quality_engineer",
        _route_after_qa,
        {"developer": "developer", "end": END},
    )

    return workflow.compile()
