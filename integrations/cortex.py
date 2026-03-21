"""Snowflake Cortex LLM client factory.

All agent nodes obtain their LLM instance through ``get_cortex_llm`` so that
API credentials are sourced from environment variables in a single place.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from config import settings


def get_cortex_llm(
    model: str = "deepseek-r1",
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> ChatOpenAI:
    """Create a Snowflake Cortex LLM client.

    Reads credentials from the environment:
      - ``SNOWFLAKE_API_KEY``  — JWT token for the Cortex endpoint.
      - ``SNOWFLAKE_API_BASE`` — Base URL, e.g.
        ``https://<account>.snowflakecomputing.com/api/v2/cortex/v1``.

    Args:
        model: The Cortex model name to use (e.g. ``"deepseek-r1"``).
        temperature: Sampling temperature; lower = more deterministic.
        max_tokens: Maximum number of tokens in the completion.

    Returns:
        A configured ``ChatOpenAI`` instance pointing at the Cortex API.

    Raises:
        EnvironmentError: If either required environment variable is unset.
    """
    api_key = settings.snowflake_api_key
    api_base = settings.snowflake_api_base

    if not api_key:
        raise OSError("SNOWFLAKE_API_KEY is not set. Add it to your .env file.")
    if not api_base:
        raise OSError(
            "SNOWFLAKE_API_BASE is not set. Add it to your .env file."
        )

    return ChatOpenAI(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        openai_api_key=api_key,
        openai_api_base=api_base,
    )
