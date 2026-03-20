"""LangGraph workflow — Architect only (test mode)."""

from __future__ import annotations

from langgraph.graph import StateGraph, END

from state import AgentState
from agents.architect import architect_node


def build_graph() -> StateGraph:
    workflow = StateGraph(AgentState)
    workflow.add_node("architect", architect_node)
    workflow.set_entry_point("architect")
    workflow.add_edge("architect", END)
    return workflow.compile()
