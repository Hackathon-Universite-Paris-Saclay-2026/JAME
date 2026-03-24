"""Learning mode prompts — loaded from learning_mode.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("learning_mode")

ANALYZE_PROMPT: str = _p["exercise_generator"]["analyze"]
STRIP_PROMPT: str = _p["exercise_generator"]["strip"]

REVIEW_PROMPT: str = _p["validator"]["review"]
HINT_LEVEL_1_PROMPT: str = _p["validator"]["hints"]["level_1"]
HINT_LEVEL_2_PROMPT: str = _p["validator"]["hints"]["level_2"]
HINT_LEVEL_3_PROMPT: str = _p["validator"]["hints"]["level_3"]
