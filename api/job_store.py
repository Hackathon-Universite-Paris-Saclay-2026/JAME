"""In-memory run store with pub-sub for WebSocket event streaming."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
import json

from cancel_token import CancelToken

from .models import ReasoningEvent, RunCreateRequest, RunRecord, RunStatus


class InMemoryRunStore:
    """Stores run records and provides pub-sub for WebSocket clients."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._subscribers: dict[str, list[asyncio.Queue[ReasoningEvent]]] = (
            defaultdict(list)
        )
        self._cancelled: set[str] = set()
        self._tokens: dict[str, CancelToken] = {}
        self._chunk_queues: dict[str, asyncio.Queue] = {}

    def create_run(self, run_id: str, request: RunCreateRequest) -> RunRecord:
        """Create and persist a new run record in pending state."""
        now = datetime.now(UTC)
        record = RunRecord(
            run_id=run_id,
            status=RunStatus.PENDING,
            created_at=now,
            updated_at=now,
            request=request,
        )
        self._runs[run_id] = record
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return a run record by id, or None when it does not exist."""
        return self._runs.get(run_id)

    def update_status(self, run_id: str, status: RunStatus) -> None:
        """Update the run status and touch the updated timestamp."""
        record = self._runs[run_id]
        record.status = status
        record.updated_at = datetime.now(UTC)

    def set_result(self, run_id: str, result: dict) -> None:
        """Store the final run result payload and update timestamp."""
        record = self._runs[run_id]
        record.result = result
        record.updated_at = datetime.now(UTC)

    def set_error(self, run_id: str, error: str) -> None:
        """Store a terminal error message and mark the run as failed."""
        record = self._runs[run_id]
        record.error = error
        record.status = RunStatus.FAILED
        record.updated_at = datetime.now(UTC)

    def register_token(
        self, run_id: str, token: CancelToken, chunk_queue: asyncio.Queue
    ) -> None:
        """Register cancellation primitives for a specific run id."""
        self._tokens[run_id] = token
        self._chunk_queues[run_id] = chunk_queue

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a running run — fires the token AND unblocks the async queue immediately."""
        record = self._runs.get(run_id)
        if record and record.status == RunStatus.RUNNING:
            self._cancelled.add(run_id)
            token = self._tokens.get(run_id)
            if token:
                token.cancel()
            # Unblock the async chunk_queue.get() immediately so the pipeline
            # stops without waiting for the current LLM call to finish.
            queue = self._chunk_queues.get(run_id)
            if queue:
                queue.put_nowait(("cancelled", None))
            return True
        return False

    def is_cancelled(self, run_id: str) -> bool:
        """Return whether cancellation has been requested for the run."""
        return run_id in self._cancelled

    def artifacts_for(self, run_id: str) -> dict[str, str]:
        """Build artifact paths for a completed run."""
        record = self._runs.get(run_id)
        if record is None:
            return {}
        output_dir = record.result.get("output_dir", "")
        if not output_dir:
            return {}
        artifacts = {
            "output_dir": output_dir,
            "project_dir": f"{output_dir}/project",
            "reasoning_trace": f"{output_dir}/reasoning_trace.json",
            "specifications": f"{output_dir}/specifications.md",
            "diagrams": f"{output_dir}/c4_diagrams.md",
        }
        generated = record.result.get("generated_files", [])
        if generated:
            artifacts["generated_files"] = json.dumps(generated)
        return artifacts

    async def publish(self, event: ReasoningEvent) -> None:
        """Publish an event to all subscribers for the event's run id."""
        for queue in list(self._subscribers[event.run_id]):
            await queue.put(event)

    def subscribe(self, run_id: str) -> asyncio.Queue[ReasoningEvent]:
        """Create and register a subscriber queue for a run."""
        queue: asyncio.Queue[ReasoningEvent] = asyncio.Queue()
        self._subscribers[run_id].append(queue)
        return queue

    def unsubscribe(
        self, run_id: str, queue: asyncio.Queue[ReasoningEvent]
    ) -> None:
        """Remove a previously registered subscriber queue for a run."""
        listeners = self._subscribers.get(run_id, [])
        if queue in listeners:
            listeners.remove(queue)
