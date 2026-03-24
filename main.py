"""FastAPI application entry point — mounts the JAME orchestrator API."""

from __future__ import annotations

from api.app import app  # re-exported so tests can import from 'main'
from config import settings


__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
