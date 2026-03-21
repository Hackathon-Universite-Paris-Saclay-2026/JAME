"""LLM system prompts for every agent node in the pipeline."""

from pathlib import Path
from typing import Any

import yaml


_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"


def load_prompts(name: str) -> dict[str, Any]:
    """Load a YAML prompt file from the prompts directory.

    Args:
        name: File stem (without .yaml extension).

    Returns:
        Parsed YAML content as a dict.
    """
    with open(_PROMPTS_DIR / f"{name}.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
