"""QA agent prompts — loaded from quality_engineer.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("quality_engineer")

TRIAGE_PROMPT: str             = _p["triage"]["classify_files"]
STATIC_ANALYSIS_PROMPT: str    = _p["review"]["static_analysis"]
CROSS_FILE_PROMPT: str         = _p["review"]["cross_file"]
PATCH_INSTRUCTIONS_PROMPT: str = _p["fix"]["patch_instructions"]
FULL_REWRITE_PROMPT: str       = _p["fix"]["full_rewrite"]
RE_REVIEW_PROMPT: str          = _p["validation"]["re_review"]
VERDICT_PROMPT: str            = _p["validation"]["verdict"]
COMPRESS_PROMPT: str           = _p["memory"]["compress"]
