"""Application configuration — loads .env and exposes typed settings."""

from __future__ import annotations

from dataclasses import dataclass, field
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Typed settings sourced from environment variables (and .env)."""

    snowflake_api_key: str = field(
        default_factory=lambda: os.getenv("SNOWFLAKE_API_KEY", "")
    )
    snowflake_api_base: str = field(
        default_factory=lambda: os.getenv("SNOWFLAKE_API_BASE", "")
    )
    host: str = field(
        default_factory=lambda: os.getenv("JAME_HOST", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: int(os.getenv("JAME_PORT", "8000"))
    )


settings = Settings()
