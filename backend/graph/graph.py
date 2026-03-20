"""LangGraph workflow — orchestrates the multi-agent pipeline.

Flow:
  user_request → PM → Developer → QA ─┐
                          ↑             │
                          └── (FAIL) ───┘
                                │
                             (PASS) → DevOps → END

The QA → Developer loop is capped at ``max_iterations`` to prevent infinite loops.
"""
