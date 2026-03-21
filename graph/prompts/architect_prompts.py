"""Architect agent prompts — loaded from solution_architect.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("solution_architect")

# Backward-compat alias
ARCHITECT_SYSTEM_PROMPT: str = _p["design"]["initial"]

SCOPE_CLASSIFIER_PROMPT: str = _p["analysis"]["scope_classifier"]
INTERROGATION_ROUND_PROMPT: str = _p["analysis"]["interrogation_round"]

DESIGN_INITIAL_PROMPT: str = _p["design"]["initial"]
DESIGN_REVISION_PROMPT: str = _p["design"]["revision"]
DESIGN_LIGHTWEIGHT_PROMPT: str = _p["design"]["lightweight"]

SELF_CRITIQUE_PROMPT: str = _p["quality"]["self_critique"]
VALIDATION_PROMPT: str = _p["interaction"]["validation"]
MEMORY_COMPRESS_PROMPT: str = _p["memory"]["compress"]
