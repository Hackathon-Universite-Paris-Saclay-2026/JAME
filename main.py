"""FastAPI application entry point — mounts the JAME orchestrator API."""

from __future__ import annotations

from api.app import app as app  # re-export for uvicorn main:app
from config import settings


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
