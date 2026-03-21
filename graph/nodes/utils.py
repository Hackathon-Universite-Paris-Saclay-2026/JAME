"""Shared utilities for all agent nodes."""

from __future__ import annotations

import json
import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

COMPRESS_THRESHOLD = 5000


def strip_thinking(text: str) -> tuple[str, str]:
    """Extract <think>…</think> blocks produced by reasoning models.

    Args:
        text: Raw LLM response string.

    Returns:
        A (thinking, content) tuple. ``thinking`` is empty if no block found.
    """
    if "<think>" in text and "</think>" in text:
        thinking = text.split("<think>")[1].split("</think>")[0].strip()
        content = text.split("</think>", 1)[-1].strip()
        return thinking, content
    return "", text


def parse_json_safe(raw: str, fallback: dict | None = None) -> dict:
    """Extract JSON from an LLM response, stripping markdown fences if present.

    Tries a direct parse first, then falls back to regex extraction.

    Args:
        raw: Raw LLM response string.
        fallback: Value to return if no valid JSON is found. Defaults to ``{}``.

    Returns:
        Parsed dict, or ``fallback`` if parsing fails.
    """
    if fallback is None:
        fallback = {}
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return fallback


def compress(llm: BaseChatModel, context: str, prompt_template: str) -> str:
    """Compress a context string using the LLM.

    Args:
        llm: The language model to use.
        context: The context string to compress.
        prompt_template: A format string with a ``{context}`` placeholder.

    Returns:
        Compressed context string.
    """
    print("[MEMORY] Context too large — compressing …")
    resp = llm.invoke(
        [HumanMessage(content=prompt_template.format(context=context))]
    )
    _, compressed = strip_thinking(resp.content)
    print("[MEMORY] Compressed.\n")
    return compressed


def maybe_compress(
    llm: BaseChatModel,
    new_context: str,
    memory: str,
    prompt_template: str,
    threshold: int = COMPRESS_THRESHOLD,
) -> str:
    """Compress only when the combined memory + new context exceeds the threshold.

    Args:
        llm: The language model to use.
        new_context: Newly produced context to append.
        memory: Existing memory string.
        prompt_template: A format string with a ``{context}`` placeholder.
        threshold: Character count above which compression is triggered.

    Returns:
        Updated memory string, compressed if necessary.
    """
    full = (memory + "\n\n" + new_context).strip()
    return compress(llm, full, prompt_template) if len(full) > threshold else full
