"""QA agent prompts — loaded from quality_engineer.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("quality_engineer")

SCOPE_DECISION_PROMPT: str = _p["scope_decision"]["classify"]
STATIC_ANALYSIS_PROMPT: str = _p["review"]["static_analysis"]
CROSS_FILE_PROMPT: str = _p["review"]["cross_file"]
PATCH_INSTRUCTIONS_PROMPT: str = _p["fix"]["patch_instructions"]
FULL_REWRITE_PROMPT: str = _p["fix"]["full_rewrite"]
TEST_RELEVANCY_PROMPT: str = _p["test_relevancy"]["assess"]
PLAN_REVIEW_PROMPT: str = _p["plan_review"]["structure"]
PRUNE_TESTS_PROMPT: str = _p["prune_tests"]["rewrite"]
SPECS_COMPARISON_PROMPT: str = _p["specs_comparison"]["compare"]
COMPRESS_PROMPT: str = _p["memory"]["compress"]
