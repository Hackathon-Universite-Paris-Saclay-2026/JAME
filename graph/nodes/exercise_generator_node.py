"""Stripper node — Learning Mode Ghost Agent.

Transforms the Developer's golden solution into a learning exercise by:

  [ANALYZE]  Identify which functions/methods contain non-trivial business
             logic worth implementing as a learning objective.
  [STRIP]    Replace each identified block with a valid typed stub + TODO
             comment, so the IDE's LSP remains happy (no red squiggles).
  [PACKAGE]  Emit golden_files (hidden reference), exercise_files (stubs),
             learning_objectives, and progressive hints for each block.

Files never stripped: config, tests, frontend, entry points.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from cancel_token import raise_if_cancelled
from graph.prompts.learning_prompts import ANALYZE_PROMPT, STRIP_PROMPT
from graph.state import AgentState, CodeFile
from integrations.cortex import get_cortex_llm
from utils.node import parse_json_safe, strip_thinking


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SKIP_PATTERNS = (
    "requirements",
    ".env",
    "pyproject",
    "makefile",
    ".gitignore",
    "dockerfile",
    "docker-compose",
    ".github",
    ".yml",
    ".yaml",
    ".json",
    ".md",
    ".txt",
    ".html",
    ".css",
    "frontend/",
    "tests/",
    "test_",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _should_skip(path: str) -> bool:
    """Return True for files that should never be stripped."""
    lower = path.lower()
    return any(pat in lower for pat in _SKIP_PATTERNS)


def _to_code_file(f: dict | CodeFile) -> CodeFile:
    if isinstance(f, CodeFile):
        return f
    return CodeFile(
        path=f["path"],
        content=f["content"],
        language=f.get("language", "python"),
    )


def _file_summary(files: list[CodeFile]) -> str:
    """Build a compact manifest for the LLM analysis prompt."""
    lines = []
    for f in files:
        lines.append(f"=== {f.path} ===\n{f.content}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def exercise_generator_node(state: AgentState) -> dict:
    """Analyze golden solution and produce exercise files with TODOs.

    Args:
        state: Current pipeline state containing ``code_files`` from the
               Developer node.

    Returns:
        Partial state update with ``golden_files``, ``exercise_files``,
        ``learning_objectives``, ``hints``, and ``reasoning_logs``.
    """
    raise_if_cancelled()

    llm = get_cortex_llm(temperature=0.2)
    logs: list[dict] = []

    raw_files = state.get("code_files", [])
    code_files = [_to_code_file(f) for f in raw_files]

    # Keep original as golden solution
    golden_files = list(code_files)

    # ── [ANALYZE] Identify learning blocks ───────────────────────────────────
    print("\n" + "=" * 60)
    print("🎓  STRIPPER AGENT — Learning Mode")
    print("=" * 60)
    print("[ANALYZE] Identifying business logic blocks to strip …")

    logs.append(
        {
            "agent": "stripper",
            "phase": "plan",
            "content": "[ANALYZE] Scanning generated files for non-trivial business logic …",
        }
    )

    strippable = [f for f in code_files if not _should_skip(f.path)]
    manifest = _file_summary(strippable)

    analyze_response = llm.invoke(
        [
            SystemMessage(content=ANALYZE_PROMPT),
            HumanMessage(content=f"Analyse these source files:\n\n{manifest}"),
        ]
    )
    raise_if_cancelled()

    _, analyze_raw = strip_thinking(analyze_response.content)

    # Parse JSON — may be array directly or wrapped
    parsed = parse_json_safe(analyze_raw, fallback=[])
    if isinstance(parsed, dict):
        # Some models wrap in {"files": [...]}
        parsed = parsed.get("files", [])
    analysis: list[dict] = parsed if isinstance(parsed, list) else []

    print(f"[ANALYZE] {len(analysis)} file(s) with learning blocks identified.")
    logs.append(
        {
            "agent": "stripper",
            "phase": "reason",
            "content": f"[ANALYZE] {len(analysis)} file(s) selected for stripping.",
        }
    )

    # ── [STRIP] Replace blocks with typed stubs ──────────────────────────────
    exercise_files: list[CodeFile] = []
    learning_objectives: list[str] = []
    hints: list[str] = []

    # Build a lookup: path → original CodeFile
    original_by_path = {f.path: f for f in code_files}

    for file_analysis in analysis:
        path = file_analysis.get("path", "")
        blocks = file_analysis.get("blocks", [])
        if not path or not blocks:
            continue

        original = original_by_path.get(path)
        if original is None:
            continue

        raise_if_cancelled()

        block_list = "\n".join(
            f"- {b['function_name']}: {b['objective']}" for b in blocks
        )
        print(f"[STRIP] {path} ({len(blocks)} block(s)) …")
        logs.append(
            {
                "agent": "stripper",
                "phase": "act",
                "content": f"[STRIP] Stripping {path} → {len(blocks)} TODO block(s).",
            }
        )

        strip_prompt = (
            f"File to strip: {path}\n\n"
            f"Functions/methods to replace with stubs:\n{block_list}\n\n"
            f"Original file content:\n{original.content}"
        )

        strip_response = llm.invoke(
            [
                SystemMessage(content=STRIP_PROMPT),
                HumanMessage(content=strip_prompt),
            ]
        )
        raise_if_cancelled()

        _, stripped_content = strip_thinking(strip_response.content)
        # Remove any accidental markdown fences
        stripped_content = stripped_content.strip()
        if stripped_content.startswith("```"):
            lines = stripped_content.splitlines()
            stripped_content = "\n".join(
                lines[1:-1] if lines[-1] == "```" else lines[1:]
            )

        exercise_files.append(
            CodeFile(
                path=original.path,
                content=stripped_content,
                language=original.language,
            )
        )

        # Collect objectives and hints from this file's blocks
        for block in blocks:
            obj = block.get("objective", "")
            reason = block.get("reason", "")
            fn = block.get("function_name", "")
            if obj:
                learning_objectives.append(f"{path}::{fn} — {obj}")
                hints.append(f"[{fn}] {reason}. Focus on: {obj}")

    # Files that were not stripped: carry them over as-is into exercise_files
    stripped_paths = {f.path for f in exercise_files}
    for f in code_files:
        if f.path not in stripped_paths:
            exercise_files.append(f)

    print(f"[PACKAGE] {len(exercise_files)} exercise file(s) ready.")
    print(f"[PACKAGE] {len(learning_objectives)} learning objective(s).")
    logs.append(
        {
            "agent": "stripper",
            "phase": "reason",
            "content": (
                f"[PACKAGE] Exercise ready — {len(learning_objectives)} objective(s) "
                f"across {len([f for f in exercise_files if not _should_skip(f.path)])} file(s)."
            ),
        }
    )

    return {
        "golden_files": golden_files,
        "exercise_files": exercise_files,
        "learning_objectives": learning_objectives,
        "hints": hints,
        "hint_level": 0,
        "validation_feedback": "",
        "reasoning_logs": logs,
    }
