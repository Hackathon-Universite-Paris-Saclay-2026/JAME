"""LLM provider abstraction for Snowflake Cortex and fallback options."""

import os
from dotenv import load_dotenv
from langchain_openai import AzureOpenAI, ChatOpenAI

# Load environment variables from .env file
load_dotenv()


def get_llm(model: str = "deepseek-r1"):
    """Get LLM instance from Snowflake Cortex (via OpenAI-compatible API)."""
    api_key = os.getenv("SNOWFLAKE_API_KEY")
    api_base = os.getenv("SNOWFLAKE_API_BASE", "https://api.openai.com/v1")

    if api_key and "cortex" in api_base.lower():
        # Use Snowflake Cortex endpoint
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=0.1,
        )
    else:
        # Fallback to OpenAI or local endpoint
        return ChatOpenAI(
            model=model or "gpt-3.5-turbo",
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            temperature=0.1,
        )
