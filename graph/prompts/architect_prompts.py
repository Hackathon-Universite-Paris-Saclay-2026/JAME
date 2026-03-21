"""Architect agent prompts — loaded from solution_architect.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("solution_architect")

ARCHITECT_SYSTEM_PROMPT: str = _p["design"]["initial"]
