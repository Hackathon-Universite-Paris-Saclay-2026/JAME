"""Route handlers — POST /generate and SSE /stream/{id}."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from graph.graph import build_graph
from graph.state import AgentState


router = APIRouter(prefix="/api", tags=["generation"])

# In-memory job store: job_id → {"status", "traces", "result", "error"}
_jobs: dict[str, dict] = {}


class GenerateRequest(BaseModel):
    """Request body for POST /generate."""

    prompt: str = Field(
        description="High-level description of the application to build."
    )
    max_iterations: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Maximum QA→Developer retry loops before forcing completion.",
    )


@router.post("/generate", status_code=202)
async def generate(request: GenerateRequest) -> dict:
    """Start a code generation job and return its ID.

    Args:
        request: Contains the user prompt and optional max_iterations.

    Returns:
        A dict with ``job_id`` to poll via GET /stream/{job_id}.
    """
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "pending", "traces": [], "result": None}
    _jobs[job_id]["task"] = asyncio.create_task(
        _run_graph(job_id, request.prompt, request.max_iterations)
    )
    return {"job_id": job_id}


@router.get("/stream/{job_id}")
async def stream_traces(job_id: str) -> StreamingResponse:
    """SSE endpoint that streams agent reasoning traces for a generation job.

    Args:
        job_id: The identifier returned by POST /generate.

    Returns:
        A ``text/event-stream`` response with one JSON event per trace entry,
        followed by a final ``complete`` event when the job finishes.

    Raises:
        HTTPException: 404 if the job_id does not exist.
    """
    if job_id not in _jobs:
        raise HTTPException(
            status_code=404, detail=f"Job '{job_id}' not found."
        )

    return StreamingResponse(
        _event_generator(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _run_graph(
    job_id: str,
    user_request: str,
    max_iterations: int,
) -> None:
    """Run the LangGraph pipeline in the background and persist traces.

    Args:
        job_id: Job identifier to update in ``_jobs``.
        user_request: The raw user prompt.
        max_iterations: Cap on QA retry loops.
    """
    _jobs[job_id]["status"] = "running"

    initial_state: AgentState = {
        "user_request": user_request,
        "specs": "",
        "diagrams": "",
        "code_files": [],
        "cicd_yaml": "",
        "dockerfile": "",
        "qa_passed": False,
        "qa_feedback": "",
        "qa_issues": [],
        "iteration": 0,
        "max_iterations": max_iterations,
        "reasoning_logs": [],
    }

    try:
        graph = build_graph()
        final_state = await asyncio.to_thread(graph.invoke, initial_state)
        _jobs[job_id]["result"] = final_state
        _jobs[job_id]["status"] = "done"
        # Persist reasoning traces so the SSE generator can flush remaining events.
        _jobs[job_id]["traces"].extend(final_state.get("reasoning_logs", []))
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)


async def _event_generator(job_id: str) -> AsyncGenerator[str, None]:
    """Yield Server-Sent Events for a job until it reaches a terminal state.

    Args:
        job_id: The job to follow.

    Yields:
        SSE-formatted strings: ``data:`` lines for trace events,
        then an ``event: complete`` line when the job finishes.
    """
    sent = 0
    while True:
        job = _jobs.get(job_id, {})
        traces = job.get("traces", [])

        # Flush any new trace entries.
        while sent < len(traces):
            yield f"data: {json.dumps(traces[sent])}\n\n"
            sent += 1

        status = job.get("status", "pending")
        if status in ("done", "error"):
            payload: dict = {"status": status}
            if status == "error":
                payload["error"] = job.get("error", "Unknown error")
            else:
                result = job.get("result") or {}
                payload["files_count"] = len(result.get("code_files", []))
            yield f"event: complete\ndata: {json.dumps(payload)}\n\n"
            break

        await asyncio.sleep(0.5)
