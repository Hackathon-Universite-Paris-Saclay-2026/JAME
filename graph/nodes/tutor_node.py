"""Tutor node — Transforms developer code into fill-in-the-blank exercises.

Active only in **Junior** mode.  Takes the QA-approved ``code_files`` and
produces ``tutor_files`` where core logic is replaced with TODO comments,
hints, and pseudo-code so the student must fill in the blanks.

The original ``code_files`` are left untouched for DevOps and artifact saving.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from cancel_token import raise_if_cancelled
from graph.prompts.tutor_prompts import TUTOR_TRANSFORM_PROMPT
from graph.state import AgentState, CodeFile
from integrations.cortex import get_cortex_llm
from utils.node import strip_thinking


def tutor_node(state: AgentState) -> dict:
    """Transform each code file into a learning exercise.

    For every file in ``state["code_files"]``, an LLM strips the core
    implementation and replaces it with guided TODO / HINT comments.

    Returns:
        Dict with ``tutor_files`` (exercise versions) and ``reasoning_logs``.
    """
    raise_if_cancelled()

    llm = get_cortex_llm(temperature=0.2, max_tokens=4096)
    code_files: list[CodeFile | dict] = state.get("code_files", [])
    tutor_files: list[dict] = []
    reasoning_logs: list[dict] = []

    reasoning_logs.append(
        {
            "agent": "tutor",
            "phase": "plan",
            "content": (
                f"Transforming {len(code_files)} file(s) into learning exercises "
                "(removing core logic, adding hints)."
            ),
        }
    )

    for cf in code_files:
        raise_if_cancelled()

        path = cf["path"] if isinstance(cf, dict) else cf.path
        content = cf["content"] if isinstance(cf, dict) else cf.content
        language = (
            cf.get("language", "python")
            if isinstance(cf, dict)
            else cf.language
        )

        # Skip non-code files (configs, requirements, etc.)
        skip_extensions = {
            ".txt",
            ".toml",
            ".cfg",
            ".ini",
            ".json",
            ".yaml",
            ".yml",
            ".md",
            ".rst",
            ".lock",
            ".gitignore",
        }
        if any(path.endswith(ext) for ext in skip_extensions):
            # Keep config files as-is in tutor output
            tutor_files.append(
                {"path": path, "content": content, "language": language}
            )
            continue

        user_msg = (
            f"File: {path}\nLanguage: {language}\n\n"
            f"```{language}\n{content}\n```"
        )

        response = llm.invoke(
            [
                SystemMessage(content=TUTOR_TRANSFORM_PROMPT),
                HumanMessage(content=user_msg),
            ]
        )

        raw = (
            response.content if hasattr(response, "content") else str(response)
        )
        thinking, transformed = strip_thinking(raw)

        # Strip any markdown fences the LLM may have added
        transformed = transformed.strip()
        if transformed.startswith("```"):
            lines = transformed.split("\n")
            # Remove first and last fence lines
            lines = lines[1:] if lines else lines
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            transformed = "\n".join(lines)

        tutor_files.append(
            {
                "path": path,
                "content": transformed,
                "language": language,
            }
        )

        reasoning_logs.append(
            {
                "agent": "tutor",
                "phase": "act",
                "content": f"Transformed {path} into exercise (blanked core logic).",
                "thinking": thinking or "",
            }
        )

    reasoning_logs.append(
        {
            "agent": "tutor",
            "phase": "reason",
            "content": (
                f"Tutor complete — {len(tutor_files)} exercise file(s) ready. "
                "Students must fill in the TODO sections to complete the solution."
            ),
        }
    )

    return {
        "tutor_files": tutor_files,
        "reasoning_logs": reasoning_logs,
    }
