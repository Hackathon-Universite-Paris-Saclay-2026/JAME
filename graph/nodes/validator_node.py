"""Validator node — Learning Mode Reviewer Agent.

Compares the junior developer's submission against the golden solution using
an LLM critic (intent-based, not string match).

  [COMPARE]  Diff golden vs submission per learning objective.
  [VERDICT]  Issue PASS / PARTIAL / FAIL with structured feedback.
  [HINTS]    If not passed, generate a progressive hint for the first
             unimplemented objective.

This node is NOT part of the main pipeline graph. It is invoked standalone
by the OrchestratorService when the junior submits via POST /runs/{id}/submit.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from graph.prompts.learning_prompts import (
    HINT_LEVEL_1_PROMPT,
    HINT_LEVEL_2_PROMPT,
    HINT_LEVEL_3_PROMPT,
    REVIEW_PROMPT,
)
from graph.state import CodeFile
from integrations.cortex import get_cortex_llm
from utils.node import parse_json_safe, strip_thinking


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_code_file(f: dict | CodeFile) -> CodeFile:
    if isinstance(f, CodeFile):
        return f
    return CodeFile(
        path=f["path"],
        content=f["content"],
        language=f.get("language", "python"),
    )


def _files_to_text(files: list[CodeFile]) -> str:
    return "\n\n".join(f"=== {f.path} ===\n{f.content}" for f in files)


def _snippet_for_objective(objective: str, golden_files: list[CodeFile]) -> str:
    """Extract the relevant snippet from golden files for a given objective."""
    # objective format: "path::function_name — description"
    parts = objective.split("::")
    if len(parts) < 2:
        return ""
    path = parts[0].strip()
    fn_part = parts[1].split("—")[0].strip()

    for f in golden_files:
        if f.path == path:
            # Extract the relevant function block (rough heuristic)
            lines = f.content.splitlines()
            in_fn = False
            snippet_lines: list[str] = []
            for line in lines:
                if f"def {fn_part}" in line or f"function {fn_part}" in line:
                    in_fn = True
                if in_fn:
                    snippet_lines.append(line)
                    # Stop at next top-level definition (indent 0) after start
                    if (
                        len(snippet_lines) > 1
                        and line
                        and not line[0].isspace()
                    ):
                        break
            if snippet_lines:
                return "\n".join(snippet_lines[:30])  # cap at 30 lines
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_submission(
    submitted_files: list[dict | CodeFile],
    golden_files: list[dict | CodeFile],
    learning_objectives: list[str],
    hints: list[str],  # noqa: ARG001
    hint_level: int,
) -> dict:
    """Validate a junior's submission against the golden solution.

    Args:
        submitted_files: Files submitted by the junior.
        golden_files: Original complete solution (hidden from junior until now).
        learning_objectives: List of objectives produced by the Exercise Packager node.
        hints: Progressive hints list produced by the Exercise Packager node.
        hint_level: Current number of hints already unlocked.

    Returns:
        Dict with keys: ``passed``, ``score``, ``feedback``,
        ``per_objective``, ``hints_unlocked``, ``hint_level``.
    """
    llm = get_cortex_llm(temperature=0.2)

    submitted = [_to_code_file(f) for f in submitted_files]
    golden = [_to_code_file(f) for f in golden_files]

    print("\n" + "=" * 60)
    print("🔍  VALIDATOR AGENT — Reviewing Submission")
    print("=" * 60)

    submission_text = _files_to_text(submitted)
    golden_text = _files_to_text(golden)
    objectives_text = "\n".join(f"- {o}" for o in learning_objectives)

    review_prompt = (
        f"LEARNING OBJECTIVES:\n{objectives_text}\n\n"
        f"GOLDEN SOLUTION:\n{golden_text}\n\n"
        f"JUNIOR SUBMISSION:\n{submission_text}"
    )

    response = llm.invoke(
        [
            SystemMessage(content=REVIEW_PROMPT),
            HumanMessage(content=review_prompt),
        ]
    )
    _, raw = strip_thinking(response.content)
    result = parse_json_safe(raw, fallback={})

    passed = bool(result.get("passed", False))
    score = int(result.get("score", 0))
    summary = result.get("summary", "Review complete.")
    per_objective = result.get("per_objective", [])
    strengths = result.get("strengths", [])
    improvements = result.get("improvements", [])

    print(f"[VERDICT] passed={passed} score={score}")

    # ── Progressive hint unlock ───────────────────────────────────────────────
    new_hint_level = hint_level
    hint_text: str | None = None

    if not passed and learning_objectives:
        # Find first unimplemented objective
        first_missing = next(
            (
                o["objective"]
                for o in per_objective
                if o.get("status") in ("partial", "missing")
            ),
            learning_objectives[0],
        )
        golden_snippet = _snippet_for_objective(first_missing, golden)

        new_hint_level = min(hint_level + 1, 3)
        hint_prompts = {
            1: HINT_LEVEL_1_PROMPT,
            2: HINT_LEVEL_2_PROMPT,
            3: HINT_LEVEL_3_PROMPT,
        }
        hint_prompt_template = hint_prompts[new_hint_level]
        current_code = next(
            (
                s.content
                for s in submitted
                if first_missing.split("::")[0] in s.path
            ),
            "",
        )

        hint_response = llm.invoke(
            [
                HumanMessage(
                    content=hint_prompt_template.format(
                        objective=first_missing,
                        current_code=current_code[:2000],
                        golden_snippet=golden_snippet,
                    )
                ),
            ]
        )
        _, hint_text = strip_thinking(hint_response.content)
        print(f"[HINT] Level {new_hint_level} hint generated.")

    feedback = summary
    if strengths:
        feedback += "\n\nStrengths:\n" + "\n".join(f"• {s}" for s in strengths)
    if improvements:
        feedback += "\n\nTo improve:\n" + "\n".join(
            f"• {i}" for i in improvements
        )

    return {
        "passed": passed,
        "score": score,
        "feedback": feedback,
        "per_objective": per_objective,
        "hint": hint_text,
        "hint_level": new_hint_level,
        "validation_feedback": feedback,
    }
