"""System prompts for the Tutor node (Junior mode)."""

from graph.prompts import load_prompts


_P = load_prompts("tutor")

TUTOR_TRANSFORM_PROMPT: str = _P["tutor"]["transform"]
