"""DevOps agent prompts — loaded from delivery_engineer.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("delivery_engineer")

DEVOPS_SYSTEM_PROMPT: str = _p["system"]
