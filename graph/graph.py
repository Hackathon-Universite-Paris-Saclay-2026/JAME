"""LangGraph workflow — orchestrates the multi-agent pipeline.

Flow (all scopes):
  user_request → Architect → Developer → QA ─┐
                               ↑             │
                               └── (FAIL) ───┘
                                             │
                                           (PASS) → DevOps → END

The QA → Developer loop is capped at ``max_iterations`` to prevent infinite loops.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from graph.nodes.architect_node import architect_node
from graph.nodes.developer_node import developer_node
from graph.nodes.devops_node import devops_node
from graph.nodes.qa_node import qa_node
from graph.nodes.stripper_node import stripper_node
from graph.state import AgentState


def _should_retry_or_continue(state: AgentState) -> str:
    """Conditional edge after QA: route back to Developer or forward to DevOps.

    Args:
        state: Current pipeline state after the QA node has run.

    Returns:
        ``"devops"`` when QA passed or the iteration cap is reached,
        ``"developer"`` otherwise.
    """
    if state.get("qa_passed"):
        print("\n✅ QA PASSED — proceeding to DevOps.")
        return "devops"

    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 3)

    if iteration >= max_iter:
        print(
            f"\n⚠️  QA FAILED but max iterations ({max_iter}) reached "
            "— proceeding to DevOps anyway."
        )
        return "devops"

    print(
        f"\n🔄 QA FAILED — routing back to Developer (iteration {iteration}/{max_iter})."
    )
    return "developer"


def build_graph() -> StateGraph:
    """Construct and compile the multi-agent LangGraph workflow.

    Returns:
        A compiled ``StateGraph`` ready to invoke with an ``AgentState``.
    """
    workflow = StateGraph(AgentState)

    # ── Nodes ────────────────────────────────────────────────────
    workflow.add_node("architect", architect_node)
    workflow.add_node("developer", developer_node)
    workflow.add_node("qa", qa_node)  # DISABLED — re-enable for production
    workflow.add_node("devops", devops_node)
    workflow.add_node("stripper", stripper_node)

    # ── Edges ────────────────────────────────────────────────────
    workflow.set_entry_point("architect")
    workflow.add_edge("architect", "developer")
    workflow.add_edge("developer", "devops")  # QA bypassed
    workflow.add_conditional_edges(
         "qa",
         _should_retry_or_continue,
         {"developer": "developer", "devops": "devops"},
    )
    workflow.add_conditional_edges(
        "devops",
        lambda s: "stripper" if s.get("learning_mode") else END,
        {"stripper": "stripper", END: END},
    )
    workflow.add_edge("stripper", END)

    return workflow.compile()
