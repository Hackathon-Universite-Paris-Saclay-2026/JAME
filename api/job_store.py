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
        self._clarification_futures: dict[str, asyncio.Future[str]] = {}
        # tool_call_id → Future["run"|"skip"]
        self._tool_futures: dict[str, asyncio.Future[str]] = {}
        # Buffer all events so late-joining WebSocket subscribers (race with pipeline start)
        # receive the full history on connect.
        self._event_buffer: dict[str, list[ReasoningEvent]] = defaultdict(list)

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
        """Cancel a running (or pending) run — fires the token AND unblocks the async queue immediately.

        Also handles the AWAITING_SPECS_REVIEW state where the pipeline is blocked
        waiting for human approval: cancels the clarification future so the architect
        unblocks, then fires the cancel token so the run terminates cleanly.
        """
        record = self._runs.get(run_id)
        cancellable = (
            RunStatus.RUNNING,
            RunStatus.PENDING,
            RunStatus.AWAITING_SPECS_REVIEW,
        )
        if record and record.status in cancellable:
            self._cancelled.add(run_id)
            token = self._tokens.get(run_id)
            if token:
                token.cancel()
            # Unblock the async chunk_queue.get() immediately so the pipeline
            # stops without waiting for the current LLM call to finish.
            queue = self._chunk_queues.get(run_id)
            if queue:
                queue.put_nowait(("cancelled", None))
            # If blocked waiting for specs approval, unblock the clarification future
            # so the architect thread doesn't hang indefinitely.
            future = self._clarification_futures.pop(run_id, None)
            if future and not future.done():
                future.cancel()
            return True
        return False

    def is_cancelled(self, run_id: str) -> bool:
        """Return whether cancellation has been requested for the run."""
        return run_id in self._cancelled

    def request_clarification(self, run_id: str) -> asyncio.Future[str]:
        """Create and store an asyncio Future that will be resolved when the user answers."""
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._clarification_futures[run_id] = future
        return future

    def submit_clarification(self, run_id: str, answer: str) -> bool:
        """Resolve the pending clarification future with the user's answer.

        Returns True if a pending future was found and resolved.
        """
        future = self._clarification_futures.pop(run_id, None)
        if future and not future.done():
            future.set_result(answer)
            return True
        return False

    def request_tool_call(self, tool_call_id: str) -> asyncio.Future[str]:
        """Create and store a Future resolved when the user responds run/skip."""
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._tool_futures[tool_call_id] = future
        return future

    def submit_tool_response(self, tool_call_id: str, action: str) -> bool:
        """Resolve a pending tool_call future with 'run' or 'skip'.

        Returns True if a pending future was found and resolved.
        """
        future = self._tool_futures.pop(tool_call_id, None)
        if future and not future.done():
            future.set_result(action)
            return True
        return False

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

    TERMINAL_EVENTS: frozenset[str] = frozenset(
        {"run_completed", "run_failed", "run_cancelled"}
    )

    async def publish(self, event: ReasoningEvent) -> None:
        """Publish an event to all subscribers for the event's run id.

        Also buffers the event so late-joining subscribers receive the full
        history. After a terminal event the sentinel ``None`` is pushed so
        WebSocket consumers can exit their read loop cleanly.
        """
        self._event_buffer[event.run_id].append(event)
        for queue in list(self._subscribers[event.run_id]):
            await queue.put(event)
            if event.event in self.TERMINAL_EVENTS:
                await queue.put(None)  # type: ignore[arg-type]

    def subscribe(self, run_id: str) -> asyncio.Queue[ReasoningEvent]:
        """Create and register a subscriber queue, pre-filled with buffered events.

        Any events already published before this subscribe() call are replayed
        immediately so the WebSocket handler never misses the pipeline start.
        """
        queue: asyncio.Queue[ReasoningEvent] = asyncio.Queue()
        # Replay buffered history first
        for past_event in self._event_buffer.get(run_id, []):
            queue.put_nowait(past_event)
            if past_event.event in self.TERMINAL_EVENTS:
                queue.put_nowait(None)  # type: ignore[arg-type]
        self._subscribers[run_id].append(queue)
        return queue

    def has_subscribers(self, run_id: str) -> bool:
        """Return True if at least one WebSocket client is subscribed to this run."""
        return bool(self._subscribers.get(run_id))

    def unsubscribe(
        self, run_id: str, queue: asyncio.Queue[ReasoningEvent]
    ) -> None:
        """Remove a previously registered subscriber queue for a run."""
        listeners = self._subscribers.get(run_id, [])
        if queue in listeners:
            listeners.remove(queue)
