"""QA node — Validates generated code against specifications.

Returns structured "actionable tickets" (list[QAIssue]) instead of
free-form text so the Developer node can address issues precisely.
Routes failures back to the Developer node via the graph's conditional edge.
"""
