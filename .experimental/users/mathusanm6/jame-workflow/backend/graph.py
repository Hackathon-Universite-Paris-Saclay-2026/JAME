"""LangGraph workflow — AI-DLC pipeline.

AI-DLC flow:
  INCEPTION
    user_request → Architect (scope classification + specs)
  CONSTRUCTION
    Architect → Developer (code generation)
             → [Delivery Engineer — only for system/product scope]
             → Quality Engineer (QA loop)
                ↑                    │ FAIL + iterations left
                └────────────────────┘
                         │ PASS (or max iterations reached)
                         ↓
                       END

Delivery Engineer is skipped for "function" and "feature" scope — no CI/CD or
Dockerfile is needed for a single algorithm or isolated feature.
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END

from state import AgentState
from agents.quality_engineer import quality_engineer_node
from agents.architect import architect_node, developer_node, delivery_engineer_node


# ── Routing helpers ───────────────────────────────────────────────────────────

_SIMPLE_SCOPES = {"function", "feature"}


def _route_after_developer(state: AgentState) -> str:
    """Skip Delivery Engineer for simple (function/feature) scopes."""
    scope = state.get("scope", "system")
    if scope in _SIMPLE_SCOPES:
        print(f"[GRAPH] Scope={scope} — skipping Delivery Engineer.")
        return "quality_engineer"
    return "delivery_engineer"


def _route_after_qa(state: AgentState) -> str:
    """Loop back to Developer on QA FAIL, exit on PASS or max iterations."""
    if state.get("qa_passed"):
        print("\n[GRAPH] QA decision: PASS — pipeline complete.")
        return "end"

    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 3)

    if iteration >= max_iterations:
        print(f"\n[GRAPH] QA decision: FAIL — max iterations ({max_iterations}) reached.")
        return "end"

    print(f"\n[GRAPH] QA decision: FAIL — routing to Developer (iteration {iteration}/{max_iterations}).")
    return "developer"


# ── Graph construction ────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """Build and compile the AI-DLC multi-agent LangGraph workflow."""

    workflow = StateGraph(AgentState)

    workflow.add_node("architect", architect_node)
    workflow.add_node("developer", developer_node)
    workflow.add_node("delivery_engineer", delivery_engineer_node)
    workflow.add_node("quality_engineer", quality_engineer_node)

    workflow.set_entry_point("architect")
    workflow.add_edge("architect", "developer")

    # After developer: skip delivery for simple scopes
    workflow.add_conditional_edges(
        "developer",
        _route_after_developer,
        {"delivery_engineer": "delivery_engineer", "quality_engineer": "quality_engineer"},
    )

    workflow.add_edge("delivery_engineer", "quality_engineer")

    workflow.add_conditional_edges(
        "quality_engineer",
        _route_after_qa,
        {"developer": "developer", "end": END},
    )

    return workflow.compile()
