"""QA agent prompts — loaded from quality_engineer.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("quality_engineer")

QA_SYSTEM_PROMPT: str = _p["system"]
