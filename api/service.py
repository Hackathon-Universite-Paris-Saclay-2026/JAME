"""Orchestrator service — runs the LangGraph pipeline and streams events via WebSocket."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import traceback
from typing import Any
import uuid

from pydantic import BaseModel

from cancel_token import CancelToken, RunCancelledError, set_current_token
from graph.graph import build_graph, build_graph_from
from graph.nodes.validator_node import validate_submission as _validate
from graph.state import AgentState
from utils.node import finalize_artifacts

from .job_store import InMemoryRunStore
from .models import ReasoningEvent, RunCreateRequest, RunStatus


def _pydantic_encoder(obj: Any) -> Any:
    """JSON encoder that serialises Pydantic models as dicts instead of str()."""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    return str(obj)


class OrchestratorService:
    """Creates and manages LangGraph pipeline runs, streaming events to WebSocket clients."""

    def __init__(self, output_root: Path) -> None:
        self.store = InMemoryRunStore()
        self.output_root = output_root
        self._run_tasks: set[asyncio.Task[None]] = set()

    def _track_task(self, task: asyncio.Task[None]) -> None:
        """Keep a strong reference to background tasks until they finish."""
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)

    async def create_run(self, request: RunCreateRequest) -> str:
        """Create a new run and launch the pipeline as a background task."""
        run_id = str(uuid.uuid4())
        self.store.create_run(run_id, request)
        task = asyncio.create_task(self._run_pipeline(run_id, request))
        self._track_task(task)
        return run_id

    async def _finalize_run(
        self, run_id: str, run_output_dir: Path, final_state: AgentState
    ) -> None:
        """Persist artifacts, store result, and emit the terminal event."""
        output_dir = str(run_output_dir / "output")
        finalize_artifacts(final_state, output_dir)

        project_dir = (run_output_dir / "output" / "project").resolve()
        generated_files: list[str] = []
        if final_state.get("mode") != "junior":
            for code_file in final_state.get("code_files", []):
                rel_path = (
                    code_file.get("path", "")
                    if isinstance(code_file, dict)
                    else code_file.path
                )
                if rel_path:
                    generated_files.append(str((project_dir / rel_path).resolve()))

        serialisable_state = {
            k: v
            for k, v in final_state.items()
            if k not in ("clarification_callback", "emit_callback", "tool_call_callback")
        }
        result = {
            "qa_passed": final_state.get("qa_passed", False),
            "qa_feedback": final_state.get("qa_feedback", ""),
            "reasoning_logs": final_state.get("reasoning_logs", []),
            "output_dir": str((run_output_dir / "output").resolve()),
            "project_dir": str(project_dir),
            "generated_files": generated_files,
            "state": json.loads(
                json.dumps(serialisable_state, default=_pydantic_encoder)
            ),
        }
        self.store.set_result(run_id, result)

        if final_state.get("mode") == "junior" and final_state.get("exercise_files"):
            self.store.update_status(run_id, RunStatus.AWAITING_SUBMISSION)
            await self._emit(
                run_id,
                "awaiting_submission",
                "Pipeline complete — waiting for your implementation.",
                payload={
                    "learning_objectives": final_state.get("learning_objectives", []),
                    "hints_available": len(final_state.get("hints", [])),
                },
            )
            return

        self.store.update_status(run_id, RunStatus.SUCCEEDED)
        await self._emit(
            run_id,
            "run_completed",
            "Build completed successfully!",
            payload={
                "output_dir": result["output_dir"],
                "project_dir": result["project_dir"],
                "generated_files": result["generated_files"],
                "qa_passed": result["qa_passed"],
            },
        )

    def cancel_run(self, run_id: str) -> bool:
        """Request cancellation of a running pipeline."""
        return self.store.cancel_run(run_id)

    @staticmethod
    def _detect_resume_node(state: dict) -> str:
        """Detect the earliest node that did not complete based on saved state.

        Inspects which outputs are populated to determine the last successful
        node, and returns the *next* node that should run.

        Returns:
            Node name to resume from: one of architect / developer / qa / devops.
        """
        # If code_files exist, developer completed — resume from qa
        if state.get("code_files"):
            # If qa_passed is True or qa_feedback exists, qa completed — resume from devops
            if state.get("qa_passed") or state.get("qa_feedback"):
                return "devops"
            return "qa"
        # If specs exist, architect completed — resume from developer
        if state.get("specs"):
            return "developer"
        # Nothing completed — start fresh from architect
        return "architect"

    async def resume_run(self, run_id: str) -> str | None:
        """Resume a failed run from the last successful checkpoint.

        Reconstructs the pipeline state from the saved snapshot and re-runs
        the graph starting from the node that failed.

        Args:
            run_id: ID of the failed run.

        Returns:
            The new run_id for the resumed run, or None if the run is not resumable.
        """
        record = self.store.get_run(run_id)
        if record is None or record.status != RunStatus.FAILED:
            return None
        saved_state = record.result.get("state")
        if not saved_state:
            return None

        entry_node = self._detect_resume_node(saved_state)
        new_run_id = str(uuid.uuid4())

        # Create a new record linked to the same request
        self.store.create_run(new_run_id, record.request)

        asyncio.create_task(
            self._run_pipeline_resume(new_run_id, record.request, saved_state, entry_node)
        )
        return new_run_id

    async def _run_pipeline_resume(
        self,
        run_id: str,
        request: RunCreateRequest,
        saved_state: dict,
        entry_node: str,
    ) -> None:
        """Run the pipeline from *entry_node* using *saved_state* as the starting point."""
        self.store.update_status(run_id, RunStatus.RUNNING)
        await self._emit(
            run_id,
            "run_started",
            f"Resuming from {entry_node}…",
        )

        token = CancelToken()
        chunk_queue: asyncio.Queue = asyncio.Queue()
        self.store.register_token(run_id, token, chunk_queue)
        if self.store.is_cancelled(run_id):
            token.cancel()
            chunk_queue.put_nowait(("cancelled", None))

        loop = asyncio.get_running_loop()
        clarify_fn = self._make_clarify_fn(run_id, loop)
        emit_fn = self._make_emit_fn(run_id, loop)
        tool_call_fn = self._make_tool_call_fn(run_id, loop)

        run_output_dir = (self.output_root / run_id).resolve()
        run_output_dir.mkdir(parents=True, exist_ok=True)

        # Reconstruct AgentState from snapshot, injecting fresh callbacks
        resume_state: AgentState = {
            **saved_state,  # type: ignore[misc]
            "run_output_dir": str(run_output_dir),
            "clarification_callback": clarify_fn,
            "emit_callback": emit_fn,
            "tool_call_callback": tool_call_fn,
            "reasoning_logs": [],  # fresh trace for this resumed run
            "max_iterations": request.max_iterations,
        }

        final_state: AgentState = resume_state

        def _run_graph_sync() -> None:
            set_current_token(token)
            graph = build_graph_from(entry_node)
            try:
                for chunk in graph.stream(resume_state, stream_mode="updates"):
                    loop.call_soon_threadsafe(
                        chunk_queue.put_nowait, ("chunk", chunk)
                    )
            except RunCancelledError:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("cancelled", None))
            except Exception as exc:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("error", exc))
            else:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("done", None))

        # Reuse the same chunk-processing loop as _run_pipeline by delegating
        # — simplest: just duplicate the await loop inline (it's self-contained)
        try:
            graph_task = asyncio.create_task(asyncio.to_thread(_run_graph_sync))

            while True:
                kind, payload = await chunk_queue.get()

                if kind == "cancelled":
                    await self._emit(run_id, "run_cancelled", "Build cancelled by user.")
                    self.store.update_status(run_id, RunStatus.CANCELLED)
                    return

                if kind == "error":
                    self._raise_pipeline_error(payload)

                if kind == "done":
                    break

                chunk = payload
                for node_name, node_update in chunk.items():
                    if not isinstance(node_update, dict):
                        continue
                    final_state = self._merge_state(final_state, node_update)

                    if node_name == "architect":
                        diagrams = node_update.get("diagrams", "")
                        specs = node_update.get("specs", "")
                        if diagrams or specs:
                            await self._emit(
                                run_id=run_id,
                                event="architect_done",
                                message="Architecture design complete.",
                                agent="architect",
                                phase="design",
                                payload={
                                    "specs": specs,
                                    "diagrams": diagrams,
                                    "scope": node_update.get("scope", ""),
                                },
                            )

                    if node_name == "developer" and node_update.get("code_files"):
                        dev_files = node_update["code_files"]
                        project_dir_node = (run_output_dir / "output" / "project").resolve()
                        emitted_paths: list[str] = []
                        for cf in dev_files:
                            p = cf.get("path", "") if isinstance(cf, dict) else cf.path
                            if p:
                                emitted_paths.append(str(project_dir_node / p))
                        await self._emit(
                            run_id=run_id,
                            event="files_ready",
                            message=f"{len(emitted_paths)} file(s) ready",
                            payload={
                                "generated_files": emitted_paths,
                                "project_dir": str(project_dir_node),
                            },
                        )

            await self._finalize_run(run_id, run_output_dir, final_state)

        except Exception as exc:
            error_msg = f"{exc!s}\n{traceback.format_exc()}"
            print(f"[ERROR] Resumed run {run_id} failed: {error_msg}", file=sys.stderr)
            if final_state:
                serialisable_state = {
                    k: v
                    for k, v in final_state.items()
                    if k not in ("clarification_callback", "emit_callback", "tool_call_callback")
                }
                self.store.set_result(
                    run_id,
                    {
                        "state": json.loads(json.dumps(serialisable_state, default=_pydantic_encoder)),
                        "output_dir": str((run_output_dir / "output").resolve()),
                        "project_dir": str((run_output_dir / "output" / "project").resolve()),
                        "reasoning_logs": final_state.get("reasoning_logs", []),
                        "resumable": True,
                    },
                )
            self.store.set_error(run_id, str(exc))
            await self._emit(
                run_id,
                "run_failed",
                f"Build failed: {exc!s}",
                payload={"error": error_msg, "resumable": bool(final_state)},
            )
        finally:
            if not graph_task.done():
                graph_task.cancel()

    async def validate_submission(
        self, run_id: str, submitted_files: list[dict]
    ) -> dict:
        """Run the Validator agent on the junior's submission.

        Args:
            run_id: ID of the learning mode run.
            submitted_files: Files submitted by the junior developer.

        Returns:
            Dict with keys: passed, score, feedback, per_objective, hint, hint_level.
        """
        record = self.store.get_run(run_id)
        if record is None:
            return {"passed": False, "score": 0, "feedback": "Run not found."}

        state = record.result.get("state", {})
        golden_files = state.get("golden_files", [])
        learning_objectives = state.get("learning_objectives", [])
        hints = state.get("hints", [])
        hint_level = state.get("hint_level", 0)

        result = await asyncio.to_thread(
            _validate,
            submitted_files,
            golden_files,
            learning_objectives,
            hints,
            hint_level,
        )

        # Persist updated hint_level into state
        state["hint_level"] = result.get("hint_level", hint_level)
        state["validation_feedback"] = result.get("feedback", "")
        record.result["state"] = state

        if result.get("passed"):
            self.store.update_status(run_id, RunStatus.SUCCEEDED)
            await self._emit(
                run_id,
                "run_completed",
                "Exercise validated — well done!",
                payload={"score": result.get("score", 0)},
            )

        return result

    async def queue_senior_prompt(self, run_id: str, prompt: str) -> bool:
        """Append an instruction to the senior prompt queue for the given run.

        Returns True if the run exists, False otherwise. The queued instruction
        will be consumed by the developer node on its next iteration.
        """
        record = self.store.get_run(run_id)
        if record is None:
            return False
        state = record.result.get("state", {})
        queue: list[str] = state.get("senior_prompt_queue", [])
        queue.append(prompt)
        state["senior_prompt_queue"] = queue
        record.result["state"] = state
        await self._emit(
            run_id,
            "prompt_queued",
            "Instruction queued for next developer iteration.",
            agent="developer",
            payload={"prompt": prompt, "queue_length": len(queue)},
        )
        return True

    async def get_next_hint(self, run_id: str) -> str | None:
        """Unlock and return the next progressive hint.

        Args:
            run_id: ID of the learning mode run.

        Returns:
            Hint text, or None if all hints are exhausted.
        """
        record = self.store.get_run(run_id)
        if record is None:
            return None

        state = record.result.get("state", {})
        hints: list[str] = state.get("hints", [])
        hint_level: int = state.get("hint_level", 0)

        if hint_level >= len(hints):
            return None

        hint = hints[hint_level]
        state["hint_level"] = hint_level + 1
        record.result["state"] = state

        await self._emit(
            run_id,
            "hint_unlocked",
            f"Hint {hint_level + 1}/{len(hints)} unlocked.",
            agent="exercise_generator",
            payload={"hint": hint, "hint_level": hint_level + 1},
        )
        return hint

    # Sentinel prefix that architect_node uses to mark specs review questions.
    _SPECS_REVIEW_PREFIX = "Please review the generated specifications."
    # Sentinel prefix that qa_node uses to mark senior iteration review questions.
    _ITERATION_REVIEW_PREFIX = "QA iteration review:"

    async def _request_clarification(
        self, run_id: str, question: str, options: list[str]
    ) -> str:
        """Emit a clarification_request event and await the user's answer (max 300s)."""
        future = self.store.request_clarification(run_id)
        await self._emit(
            run_id,
            "clarification_request",
            question,
            agent="architect",
            phase="interrogation",
            payload={"question": question, "options": options},
        )
        try:
            return await asyncio.wait_for(future, timeout=300.0)
        except TimeoutError:
            return ""

    async def _request_iteration_review(
        self, run_id: str, question: str, options: list[str]
    ) -> str:
        """Emit an iteration_review_request event and await the human's proceed/instruction answer.

        Uses the same clarification future so the existing /clarify endpoint resolves it.
        """
        future = self.store.request_clarification(run_id)
        await self._emit(
            run_id,
            "iteration_review_request",
            question,
            agent="qa",
            phase="iteration_review",
            payload={"question": question, "options": options},
        )
        try:
            return await asyncio.wait_for(future, timeout=300.0)
        except TimeoutError:
            return "proceed"

    async def _request_specs_review(
        self, run_id: str, question: str, options: list[str]
    ) -> str:
        """Emit a specs_review_request event and await the user's approve/revise answer (max 300s).

        Uses the same clarification future mechanism as regular clarification so
        the existing /runs/{run_id}/clarify endpoint can resolve it.
        Transitions the run status to AWAITING_SPECS_REVIEW while blocked,
        then back to RUNNING once the human responds.
        """
        future = self.store.request_clarification(run_id)
        # Extract specs content embedded by architect_node after the sentinel
        _sentinel = "\n\n===SPECS_CONTENT===\n"
        if _sentinel in question:
            display_question, specs_content = question.split(_sentinel, 1)
        else:
            display_question = question
            specs_content = ""
        self.store.update_status(run_id, RunStatus.AWAITING_SPECS_REVIEW)
        await self._emit(
            run_id,
            "specs_review_request",
            display_question,
            agent="architect",
            phase="specs_review",
            payload={
                "question": display_question,
                "options": options,
                "specs": specs_content,
            },
        )
        try:
            answer = await asyncio.wait_for(future, timeout=300.0)
        except (TimeoutError, asyncio.CancelledError):
            # TimeoutError: user didn't respond in time → treat as empty (auto-approve).
            # CancelledError: run was cancelled while waiting → return empty so the
            #   caller returns and the cancel token propagates RunCancelledError.
            answer = ""
        finally:
            record_after = self.store.get_run(run_id)
            if (
                record_after is not None
                and record_after.status == RunStatus.AWAITING_SPECS_REVIEW
            ):
                self.store.update_status(run_id, RunStatus.RUNNING)
        return answer

    async def _request_tool_call(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        command: str,
        args: list[str],
    ) -> str:
        """Emit a tool_call event and await the user's run/skip answer (max 300s)."""
        future = self.store.request_tool_call(tool_call_id)
        await self._emit(
            run_id,
            "tool_call",
            f"QA wants to run: {tool_name}",
            agent="qa",
            phase="BUILD_AND_TEST",
            payload={
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "command": command,
                "args": args,
            },
        )
        try:
            return await asyncio.wait_for(future, timeout=300.0)
        except TimeoutError:
            return "skip"

    def _make_tool_call_fn(
        self, run_id: str, loop: asyncio.AbstractEventLoop
    ) -> Callable[[str, str, str, list[str]], str]:
        """Return a sync callable that emits a tool_call event and blocks for run/skip."""
        svc = self

        def _tool_call_sync(
            tool_call_id: str,
            tool_name: str,
            command: str,
            args: list[str],
        ) -> str:
            coro = svc._request_tool_call(
                run_id, tool_call_id, tool_name, command, args
            )
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return future.result(timeout=305.0)
            except Exception:
                return "skip"

        return _tool_call_sync

    def _make_clarify_fn(
        self, run_id: str, loop: asyncio.AbstractEventLoop
    ) -> Callable[[str, list[str] | None], str]:
        """Return a sync callable that can be called from the graph worker thread.

        When a WebSocket client is subscribed, emits a clarification_request (or
        specs_review_request) event and waits for the answer. Falls back to stdin
        when running interactively with no WebSocket client connected (CLI mode).
        """
        svc = self

        def _clarify_sync(
            question: str, options: list[str] | None = None
        ) -> str:
            if svc.store.has_subscribers(run_id):
                # Route to the correct event type based on sentinel prefix
                if question.startswith(svc._SPECS_REVIEW_PREFIX):
                    coro = svc._request_specs_review(
                        run_id, question, options or []
                    )
                elif question.startswith(svc._ITERATION_REVIEW_PREFIX):
                    coro = svc._request_iteration_review(
                        run_id, question, options or []
                    )
                else:
                    coro = svc._request_clarification(
                        run_id, question, options or []
                    )
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                try:
                    return future.result(timeout=305.0)
                except Exception:
                    return ""
            if sys.stdin.isatty():
                print(f"\n[CLARIFY] {question}")
                if options:
                    for i, opt in enumerate(options, 1):
                        print(f"  [{i}] {opt}")
                return input("  ➜ ").strip() or "No preference / not applicable"
            return ""

        return _clarify_sync

    def _make_emit_fn(
        self, run_id: str, loop: asyncio.AbstractEventLoop
    ) -> Callable[[dict], None]:
        """Return a sync callable that emits events from node worker threads.

        Handles two event types based on the ``event`` key in the log dict:
        - ``file_generated``: streams a generated file to the UI immediately.
        - anything else (default): emits an ``agent_update`` event.
        """
        svc = self

        def _emit_sync(log: dict) -> None:
            event_type = log.get("event", "agent_update")
            if event_type == "file_generated":
                coro = svc._emit(
                    run_id=run_id,
                    event="file_generated",
                    message=log.get("message", f"Generated: {log.get('path', '')}"),
                    agent=log.get("agent", "developer"),
                    phase=log.get("phase", "CONSTRUCTION"),
                    payload={
                        "path": log.get("path", ""),
                        "content": log.get("content", ""),
                        "language": log.get("language", "text"),
                    },
                )
            else:
                coro = svc._emit(
                    run_id=run_id,
                    event="agent_update",
                    message=log.get("content", "Agent update"),
                    agent=log.get("agent"),
                    phase=log.get("phase"),
                    payload={
                        **log,
                        "thinking": log.get("thinking", ""),
                        "has_thinking": bool(log.get("thinking", "")),
                    },
                )
            asyncio.run_coroutine_threadsafe(coro, loop)

        return _emit_sync

    async def _emit(
        self,
        run_id: str,
        event: str,
        message: str,
        agent: str | None = None,
        phase: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        evt = ReasoningEvent(
            run_id=run_id,
            event=event,
            agent=agent,
            phase=phase,
            message=message,
            timestamp=datetime.now(UTC),
            payload=payload or {},
        )
        await self.store.publish(evt)

    @staticmethod
    def _merge_state(base: AgentState, update: dict[str, Any]) -> AgentState:
        merged: dict[str, Any] = dict(base)
        for key, value in update.items():
            if key == "reasoning_logs" and isinstance(value, list):
                merged[key] = [*merged.get(key, []), *value]
            else:
                merged[key] = value
        return merged  # type: ignore[return-value]

    @staticmethod
    def _raise_pipeline_error(error: Exception) -> None:
        """Raise worker exceptions in the pipeline loop context."""
        raise error

    async def _run_pipeline(
        self, run_id: str, request: RunCreateRequest
    ) -> None:
        """Run the LangGraph pipeline in a worker thread, streaming events as each node completes."""
        self.store.update_status(run_id, RunStatus.RUNNING)
        await self._emit(
            run_id, "run_started", "Starting JAME orchestration pipeline..."
        )

        token = CancelToken()
        # chunk_queue is created first so it can be registered for immediate cancel
        chunk_queue: asyncio.Queue = asyncio.Queue()
        self.store.register_token(run_id, token, chunk_queue)
        # If cancel_run() was called before register_token (race condition),
        # the run is already in the cancelled set — fire the token now.
        if self.store.is_cancelled(run_id):
            token.cancel()
            chunk_queue.put_nowait(("cancelled", None))

        loop = asyncio.get_running_loop()
        clarify_fn = self._make_clarify_fn(run_id, loop)
        emit_fn = self._make_emit_fn(run_id, loop)
        tool_call_fn = self._make_tool_call_fn(run_id, loop)

        run_output_dir = (self.output_root / run_id).resolve()
        run_output_dir.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()
        clarify_fn = self._make_clarify_fn(run_id, loop)

        initial_state: AgentState = {
            "user_request": request.user_request,
            "scope": "",
            "specs": "",
            "diagrams": "",
            "functional_design": "",
            "code_files": [],
            "ci_files": [],
            "cd_files": [],
            "needs_ci": False,
            "needs_cd": False,
            "qa_passed": False,
            "qa_feedback": "",
            "qa_issues": [],
            "run_output_dir": str(run_output_dir),
            "iteration": 0,
            "max_iterations": request.max_iterations,
            "mode": request.mode,
            "clarification_callback": clarify_fn,
            "emit_callback": emit_fn,
            "tool_call_callback": tool_call_fn,
            "reasoning_logs": [],
            # Specs review gate fields
            "specs_approved": False,
            "specs_revision_feedback": "",
            "awaiting_specs_approval": False,
            # Senior prompt queue
            "senior_prompt_queue": [],
            "golden_files": [],
            "exercise_files": [],
            "learning_objectives": [],
            "hints": [],
            "hint_level": 0,
            "validation_feedback": "",
        }

        final_state: AgentState = initial_state

        # chunk_queue already created above (registered with store for instant cancel)

        def _run_graph_sync() -> None:
            """Run LangGraph synchronously in a worker thread, streaming chunks via queue."""
            set_current_token(
                token
            )  # bind so nodes can call raise_if_cancelled()
            graph = build_graph()
            try:
                for chunk in graph.stream(initial_state, stream_mode="updates"):
                    loop.call_soon_threadsafe(
                        chunk_queue.put_nowait, ("chunk", chunk)
                    )
            except RunCancelledError:
                loop.call_soon_threadsafe(
                    chunk_queue.put_nowait, ("cancelled", None)
                )
            except Exception as exc:
                loop.call_soon_threadsafe(
                    chunk_queue.put_nowait, ("error", exc)
                )
            else:
                loop.call_soon_threadsafe(
                    chunk_queue.put_nowait, ("done", None)
                )

        try:
            graph_task = asyncio.create_task(asyncio.to_thread(_run_graph_sync))

            while True:
                kind, payload = await chunk_queue.get()

                if kind == "cancelled":
                    await self._emit(
                        run_id, "run_cancelled", "Build cancelled by user."
                    )
                    self.store.update_status(run_id, RunStatus.CANCELLED)
                    return

                if kind == "error":
                    self._raise_pipeline_error(payload)

                if kind == "done":
                    break

                # kind == "chunk" — a node has completed
                chunk = payload
                for node_name, node_update in chunk.items():
                    if not isinstance(node_update, dict):
                        continue

                    final_state = self._merge_state(final_state, node_update)

                    # After architect: emit specs + diagrams so UI can display them
                    if node_name == "architect":
                        diagrams = node_update.get("diagrams", "")
                        specs = node_update.get("specs", "")
                        if diagrams or specs:
                            await self._emit(
                                run_id=run_id,
                                event="architect_done",
                                message="Architecture design complete.",
                                agent="architect",
                                phase="design",
                                payload={
                                    "specs": specs,
                                    "diagrams": diagrams,
                                    "scope": node_update.get("scope", ""),
                                },
                            )

                    # After developer: notify UI of generated files
                    if node_name == "developer" and node_update.get(
                        "code_files"
                    ):
                        is_junior_mode = final_state.get("mode") == "junior"
                        dev_files = node_update["code_files"]
                        project_dir_node = (
                            run_output_dir / "output" / "project"
                        ).resolve()
                        emitted_paths: list[str] = []

                        if is_junior_mode:
                            await self._emit(
                                run_id=run_id,
                                event="agent_update",
                                message=(
                                    "Developer produced candidate files; "
                                    "withholding direct delivery in junior mode "
                                    "until exercise packaging completes."
                                ),
                                agent="developer",
                                phase="CONSTRUCTION",
                                payload={
                                    "withheld_files_count": len(dev_files),
                                    "iteration": final_state.get(
                                        "iteration", 0
                                    ),
                                },
                            )
                        else:
                            # Files were already streamed in real-time via emit_callback
                            # inside _run_code_generation. Just build emitted_paths and
                            # fire files_ready so the UI knows generation is complete.
                            for cf in dev_files:
                                p = (
                                    cf["path"]
                                    if isinstance(cf, dict)
                                    else cf.path
                                )
                                if p:
                                    emitted_paths.append(
                                        str(project_dir_node / p)
                                    )

                            await self._emit(
                                run_id=run_id,
                                event="files_ready",
                                message=f"{len(emitted_paths)} file(s) ready",
                                agent="developer",
                                phase="CONSTRUCTION",
                                payload={
                                    "project_dir": str(project_dir_node),
                                    "generated_files": emitted_paths,
                                    "iteration": final_state.get(
                                        "iteration", 0
                                    ),
                                },
                            )

                    # After exercise_generator: save exercise files to disk + emit event
                    if node_name == "exercise_generator":
                        exercise_files = node_update.get("exercise_files", [])
                        objectives = node_update.get("learning_objectives", [])

                        # Save exercise files under runs/{run_id}/exercise/
                        exercise_dir = (run_output_dir / "exercise").resolve()
                        exercise_dir.mkdir(parents=True, exist_ok=True)
                        exercise_paths: list[str] = []
                        for ef in exercise_files:
                            p = ef["path"] if isinstance(ef, dict) else ef.path
                            c = (
                                ef["content"]
                                if isinstance(ef, dict)
                                else ef.content
                            )
                            if p:
                                dest = exercise_dir / p
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_text(c, encoding="utf-8")
                                exercise_paths.append(str(dest))

                        # Save golden solution under runs/{run_id}/golden/
                        golden_files_state = node_update.get("golden_files", [])
                        golden_dir = (run_output_dir / "golden").resolve()
                        golden_dir.mkdir(parents=True, exist_ok=True)
                        for gf in golden_files_state:
                            p = gf["path"] if isinstance(gf, dict) else gf.path
                            c = (
                                gf["content"]
                                if isinstance(gf, dict)
                                else gf.content
                            )
                            if p:
                                dest = golden_dir / p
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_text(c, encoding="utf-8")

                        serialized = [
                            f if isinstance(f, dict) else f.model_dump()
                            for f in exercise_files
                        ]

                        # Junior mode file delivery happens only here, after stripping.
                        for ef in serialized:
                            await self._emit(
                                run_id=run_id,
                                event="file_generated",
                                message=f"Exercise generated: {ef.get('path', '')}",
                                agent="exercise_generator",
                                phase="LEARNING",
                                payload={
                                    "path": ef.get("path", ""),
                                    "content": ef.get("content", ""),
                                    "language": ef.get("language", "text"),
                                },
                            )

                        await self._emit(
                            run_id=run_id,
                            event="files_ready",
                            message=(
                                f"{len(exercise_paths)} exercise file(s) ready "
                                "for implementation"
                            ),
                            agent="exercise_generator",
                            phase="LEARNING",
                            payload={
                                "project_dir": str(exercise_dir),
                                "generated_files": exercise_paths,
                                "iteration": final_state.get("iteration", 0),
                            },
                        )

                        await self._emit(
                            run_id=run_id,
                            event="exercise_ready",
                            message=(
                                f"Learning exercise ready — "
                                f"{len(objectives)} objective(s) to implement."
                            ),
                            agent="exercise_generator",
                            phase="LEARNING",
                            payload={
                                "exercise_dir": str(exercise_dir),
                                "exercise_files": serialized,
                                "exercise_paths": exercise_paths,
                                "learning_objectives": objectives,
                                "hints_available": len(
                                    node_update.get("hints", [])
                                ),
                            },
                        )

                    # After devops: emit CI/CD file events
                    if node_name == "devops":
                        for file_list, phase_label in [
                            (node_update.get("ci_files", []), "CI"),
                            (node_update.get("cd_files", []), "CD"),
                        ]:
                            for cf in file_list:
                                p = (
                                    cf["path"]
                                    if isinstance(cf, dict)
                                    else cf.path
                                )
                                c = (
                                    cf["content"]
                                    if isinstance(cf, dict)
                                    else cf.content
                                )
                                await self._emit(
                                    run_id=run_id,
                                    event="file_generated",
                                    message=f"Generated {phase_label}: {p}",
                                    agent="devops",
                                    phase="CONSTRUCTION",
                                    payload={
                                        "path": p,
                                        "content": c,
                                        "language": "yaml"
                                        if p.endswith((".yml", ".yaml"))
                                        else "text",
                                    },
                                )

            await self._finalize_run(run_id, run_output_dir, final_state)

        except Exception as exc:
            error_msg = f"{exc!s}\n{traceback.format_exc()}"
            print(f"[ERROR] Run {run_id} failed: {error_msg}", file=sys.stderr)
            # Snapshot the partial state so the run can be resumed later
            if final_state:
                serialisable_state = {
                    k: v
                    for k, v in final_state.items()
                    if k
                    not in (
                        "clarification_callback",
                        "emit_callback",
                        "tool_call_callback",
                    )
                }
                self.store.set_result(
                    run_id,
                    {
                        "state": json.loads(
                            json.dumps(serialisable_state, default=_pydantic_encoder)
                        ),
                        "output_dir": str((run_output_dir / "output").resolve()),
                        "project_dir": str(
                            (run_output_dir / "output" / "project").resolve()
                        ),
                        "reasoning_logs": final_state.get("reasoning_logs", []),
                        "resumable": True,
                    },
                )
            self.store.set_error(run_id, str(exc))
            await self._emit(
                run_id,
                "run_failed",
                f"Build failed: {exc!s}",
                payload={"error": error_msg, "resumable": bool(final_state)},
            )
        finally:
            if not graph_task.done():
                graph_task.cancel()
