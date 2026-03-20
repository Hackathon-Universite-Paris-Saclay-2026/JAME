"""Developer node — Generates application code and tests.

Uses a two-step chunked approach to avoid token-limit and JSON-truncation
errors on large generations:

  Step 1  —  Ask the LLM for a *file plan* (list of paths to create).
  Step 2  —  Loop through those paths and call the LLM *once per file*
             to generate its content via structured output.

Architecture enforced across all generated projects:
  Router → Service → Repository/DB → Model
"""
