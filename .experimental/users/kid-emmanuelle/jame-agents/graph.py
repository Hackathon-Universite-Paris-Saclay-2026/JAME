"""LangGraph workflow — orchestrates the multi-agent pipeline.

Flow:
  user_request → Architect → Developer → QA ─┐
                                 ↑             │
                                 └── (FAIL) ───┘
                                      │
                                   (PASS) → DevOps → END

The QA → Developer loop is capped at `max_iterations` to avoid infinite loops.
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END

from state import AgentState
from agents.architect import architect_node
from agents.developer import developer_node
from agents.devops import devops_node
from agents.qa import qa_node


def should_retry_or_continue(state: AgentState) -> str:
    """Conditional edge after QA: route back to Developer or forward to DevOps."""
    if state.get("qa_passed"):
        print("\n✅ QA PASSED — proceeding to DevOps.")
        return "devops"

    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 2)

    if iteration >= max_iter:
        print(f"\n⚠️  QA FAILED but max iterations ({max_iter}) reached — proceeding to DevOps anyway.")
        return "devops"

    print(f"\n🔄 QA FAILED — routing back to Developer (iteration {iteration}/{max_iter}).")
    return "developer"


def build_graph() -> StateGraph:
    """Construct and compile the multi-agent LangGraph workflow."""

    workflow = StateGraph(AgentState)

    # ── Add nodes ───────────────────────────────────────────────
    workflow.add_node("architect", architect_node)
    workflow.add_node("developer", developer_node)
    workflow.add_node("qa", qa_node)
    workflow.add_node("devops", devops_node)

    # ── Define edges ────────────────────────────────────────────
    workflow.set_entry_point("architect")
    workflow.add_edge("architect", "developer")
    workflow.add_edge("developer", "qa")

    # Conditional: QA decides whether to loop back or move forward
    workflow.add_conditional_edges(
        "qa",
        should_retry_or_continue,
        {
            "developer": "developer",
            "devops": "devops",
        },
    )

    workflow.add_edge("devops", END)

    return workflow.compile()
