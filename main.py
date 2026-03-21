"""FastAPI application entry point — mounts routers and starts the server."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import generate


app = FastAPI(
    title="JAME - Multi-Agent Software Engineering",
    description=(
        "Generates full-stack applications via a LangGraph "
        "multi-agent pipeline (Architect → Developer → QA → DevOps)."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(generate.router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
