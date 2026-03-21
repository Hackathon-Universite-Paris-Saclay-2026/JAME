"""LangGraph workflow — DevOps-only pipeline."""

from __future__ import annotations

from langgraph.graph import StateGraph, END

from state import AgentState
from agents.devops import devops_node


def build_graph() -> StateGraph:
    """Construct and compile the DevOps-only LangGraph workflow."""

    workflow = StateGraph(AgentState)
    workflow.add_node("devops", devops_node)
    workflow.set_entry_point("devops")
    workflow.add_edge("devops", END)

    return workflow.compile()
