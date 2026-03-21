from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .models import RunCreateRequest, RunCreateResponse, RunStatusResponse
from .service import OrchestratorService

app = FastAPI(title="JAME Workflow Orchestrator", version="0.1.0")
service = OrchestratorService(output_root=Path("runs"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/runs", response_model=RunCreateResponse)
async def create_run(request: RunCreateRequest) -> RunCreateResponse:
    run_id = await service.create_run(request)
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=500, detail="Run could not be created")
    return RunCreateResponse(run_id=run_id, status=record.status)


@app.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str) -> RunStatusResponse:
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")

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
    record = service.store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    cancelled = service.cancel_run(run_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="Run is not in a cancellable state")
    return {"status": "cancellation_requested"}


@app.websocket("/ws/runs/{run_id}")
async def run_events(ws: WebSocket, run_id: str) -> None:
    if service.store.get_run(run_id) is None:
        await ws.accept()
        await ws.send_json({"event": "error", "message": "Run not found"})
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
