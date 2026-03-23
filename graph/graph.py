"""LangGraph workflow — orchestrates the multi-agent pipeline.

Supports three execution modes:

  **Expert** (default):
    Architect → [APPROVE] → Developer → QA ↔ Dev → [APPROVE] → DevOps → END
    Critical nodes (developer, devops) require human approval before executing.

  **Senior**:
    [APPROVE] → Architect → [APPROVE] → Developer → [APPROVE] → QA ↔ Dev → [APPROVE] → DevOps → END
    Every node requires human approval — full Human-in-the-Loop.

  **Junior**:
    Architect → Developer → QA ↔ Dev → Tutor → DevOps → END
    After QA passes, a Tutor node blanks out core logic for the student.

The QA → Developer loop is capped at ``max_iterations`` to prevent infinite loops.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from graph.nodes.architect_node import architect_node
from graph.nodes.developer_node import developer_node
from graph.nodes.devops_node import devops_node
from graph.nodes.qa_node import qa_node
from graph.nodes.tutor_node import tutor_node
from graph.state import AgentState


# Nodes whose execution is considered a "critical command" in Expert mode.
CRITICAL_NODES: list[str] = ["developer", "devops"]

# All pipeline nodes — used for Senior mode full Human-in-the-Loop.
ALL_NODES: list[str] = ["architect", "developer", "qa", "devops"]


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


def build_graph(mode: str = "expert") -> StateGraph:
    """Construct and compile the multi-agent LangGraph workflow.

    Args:
        mode: Execution mode — ``"expert"``, ``"senior"``, or ``"junior"``.

    Returns:
        A compiled ``StateGraph`` ready to invoke with an ``AgentState``.
    """
    workflow = StateGraph(AgentState)

    # ── Nodes (always present) ────────────────────────────────
    workflow.add_node("architect", architect_node)
    workflow.add_node("developer", developer_node)
    workflow.add_node("qa", qa_node)
    workflow.add_node("devops", devops_node)

    # ── Edges ─────────────────────────────────────────────────
    workflow.set_entry_point("architect")
    workflow.add_edge("architect", "developer")
    workflow.add_edge("developer", "qa")

    if mode == "junior":
        # Insert tutor node between QA-pass and DevOps
        workflow.add_node("tutor", tutor_node)
        workflow.add_conditional_edges(
            "qa",
            _should_retry_or_continue,
            {
                "developer": "developer",
                "devops": "tutor",  # route to tutor instead of devops
            },
        )
        workflow.add_edge("tutor", "devops")
    else:
        workflow.add_conditional_edges(
            "qa",
            _should_retry_or_continue,
            {
                "developer": "developer",
                "devops": "devops",
            },
        )

    workflow.add_edge("devops", END)

    # ── Compile with mode-specific interrupt configuration ────
    if mode == "expert":
        return workflow.compile(
            checkpointer=MemorySaver(),
            interrupt_before=CRITICAL_NODES,
        )
    if mode == "senior":
        return workflow.compile(
            checkpointer=MemorySaver(),
            interrupt_before=ALL_NODES,
        )
    # Junior — no interrupts
    return workflow.compile()
