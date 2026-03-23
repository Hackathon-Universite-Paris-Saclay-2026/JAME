"""FastAPI application — REST + WebSocket API for the JAME orchestrator."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .models import (
    ExerciseResponse,
    RunCreateRequest,
    RunCreateResponse,
    RunStatusResponse,
    SubmitRequest,
    SubmitResponse,
)
from .service import OrchestratorService


app = FastAPI(
    title="JAME — Multi-Agent Software Engineering",
    description=(
        "Generates full-stack applications via a LangGraph multi-agent pipeline "
        "(Architect → Developer → QA → DevOps). "
        "Connect via WebSocket at /ws/runs/{run_id} for real-time streaming."
    ),
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

service = OrchestratorService(output_root=Path("runs"))


@app.get("/health")
def health() -> dict[str, str]:
    """Health check endpoint used by the VS Code extension to verify the backend is ready."""
    return {"status": "ok"}


@app.post("/runs", status_code=202, response_model=RunCreateResponse)
async def create_run(request: RunCreateRequest) -> RunCreateResponse:
    """Start a new code generation pipeline run.

    Returns immediately with a run_id. Connect to /ws/runs/{run_id} to stream events.
    """
    run_id = await service.create_run(request)
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=500, detail="Run could not be created")
    return RunCreateResponse(run_id=run_id, status=record.status)


@app.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str) -> RunStatusResponse:
    """Get the status and artifacts for a completed run."""
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Run '{run_id}' not found."
        )
    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        error=record.error,
        reasoning_logs=record.result.get("reasoning_logs", []),
        artifacts=service.store.artifacts_for(run_id),
    )


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, str]:
    """Request cancellation of a running pipeline."""
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Run '{run_id}' not found."
        )
    cancelled = service.cancel_run(run_id)
    if not cancelled:
        raise HTTPException(
            status_code=409, detail="Run is not in a cancellable state."
        )
    return {"status": "cancellation_requested"}


@app.get("/runs/{run_id}/exercise", response_model=ExerciseResponse)
def get_exercise(run_id: str) -> ExerciseResponse:
    """Return the exercise files (with TODOs) for a learning mode run.

    Only available when status is ``awaiting_submission``.
    """
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Run '{run_id}' not found."
        )
    state = record.result.get("state", {})
    exercise_files = state.get("exercise_files", [])
    learning_objectives = state.get("learning_objectives", [])
    hints = state.get("hints", [])
    serialized = [
        f if isinstance(f, dict) else f.model_dump() for f in exercise_files
    ]
    return ExerciseResponse(
        run_id=run_id,
        exercise_files=serialized,
        learning_objectives=learning_objectives,
        hints_available=len(hints),
    )


@app.post("/runs/{run_id}/submit", response_model=SubmitResponse)
async def submit_solution(
    run_id: str, request: SubmitRequest
) -> SubmitResponse:
    """Submit the junior's implementation for AI review.

    Triggers the Validator agent and returns structured feedback + optional hint.
    """
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Run '{run_id}' not found."
        )
    result = await service.validate_submission(run_id, request.files)
    return SubmitResponse(**result)


@app.get("/runs/{run_id}/hint")
async def get_hint(run_id: str) -> dict:
    """Unlock and return the next progressive hint for the current exercise."""
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Run '{run_id}' not found."
        )
    hint = await service.get_next_hint(run_id)
    if hint is None:
        raise HTTPException(status_code=404, detail="No more hints available.")
    return {"hint": hint}


@app.websocket("/ws/runs/{run_id}")
async def run_events(ws: WebSocket, run_id: str) -> None:
    """WebSocket endpoint that streams ReasoningEvent objects for a run.

    Events include: run_started, agent_update, file_generated, files_ready,
    run_completed, run_failed, run_cancelled.
    """
    if service.store.get_run(run_id) is None:
        await ws.accept()
        await ws.send_json(
            {"event": "error", "message": f"Run '{run_id}' not found."}
        )
        await ws.close(code=1008)
        return

    await ws.accept()
    queue = service.store.subscribe(run_id)

    try:
        while True:
            event = await queue.get()
            await ws.send_json(event.model_dump(mode="json"))
    except WebSocketDisconnect:
        service.store.unsubscribe(run_id, queue)
