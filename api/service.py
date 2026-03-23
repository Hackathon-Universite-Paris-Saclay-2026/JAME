"""Orchestrator service — runs the LangGraph pipeline and streams events via WebSocket."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import traceback
from typing import Any
import uuid

from pydantic import BaseModel


def _pydantic_encoder(obj: Any) -> Any:
    """JSON encoder that serialises Pydantic models as dicts instead of str()."""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    return str(obj)

from cancel_token import CancelToken, RunCancelledError, set_current_token
from graph.graph import build_graph
from graph.nodes.validator_node import validate_submission as _validate
from graph.state import AgentState, CodeFile
from utils.node import save_artifacts

from .job_store import InMemoryRunStore
from .models import ReasoningEvent, RunCreateRequest, RunStatus


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

    def cancel_run(self, run_id: str) -> bool:
        """Request cancellation of a running pipeline."""
        return self.store.cancel_run(run_id)

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
            agent="stripper",
            payload={"hint": hint, "hint_level": hint_level + 1},
        )
        return hint

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
            "iteration": 0,
            "max_iterations": request.max_iterations,
            "reasoning_logs": [],
            # Learning mode fields
            "learning_mode": request.learning_mode,
            "golden_files": [],
            "exercise_files": [],
            "learning_objectives": [],
            "hints": [],
            "hint_level": 0,
            "validation_feedback": "",
        }

        run_output_dir = self.output_root / run_id
        run_output_dir.mkdir(parents=True, exist_ok=True)

        final_state: AgentState = initial_state

        # chunk_queue already created above (registered with store for instant cancel)
        loop = asyncio.get_running_loop()

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

                    # Stream reasoning log entries as agent_update events
                    logs = node_update.get("reasoning_logs", [])
                    if isinstance(logs, list) and logs:
                        for log in logs:
                            thinking = log.get("thinking", "")
                            await self._emit(
                                run_id=run_id,
                                event="agent_update",
                                message=log.get("content", "Agent update"),
                                agent=log.get("agent") or node_name,
                                phase=log.get("phase"),
                                payload={
                                    **log,
                                    "thinking": thinking,
                                    "has_thinking": bool(thinking),
                                },
                            )
                    else:
                        await self._emit(
                            run_id=run_id,
                            event="agent_update",
                            message=f"{node_name} completed.",
                            agent=node_name,
                        )

                    # After developer: write code files immediately and notify UI
                    if node_name == "developer" and node_update.get(
                        "code_files"
                    ):
                        dev_files = node_update["code_files"]
                        project_dir_early = (
                            run_output_dir / "output" / "project"
                        ).resolve()
                        project_dir_early.mkdir(parents=True, exist_ok=True)
                        saved_paths: list[str] = []

                        for cf in dev_files:
                            p = cf["path"] if isinstance(cf, dict) else cf.path
                            c = (
                                cf["content"]
                                if isinstance(cf, dict)
                                else cf.content
                            )
                            lang = (
                                cf.get("language", "text")
                                if isinstance(cf, dict)
                                else cf.language
                            )
                            if p:
                                dest = project_dir_early / p
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_text(c, encoding="utf-8")
                                saved_paths.append(str(dest))
                            await self._emit(
                                run_id=run_id,
                                event="file_generated",
                                message=f"Generated: {p}",
                                agent="developer",
                                phase="CONSTRUCTION",
                                payload={
                                    "path": p,
                                    "content": c,
                                    "language": lang,
                                },
                            )

                        await self._emit(
                            run_id=run_id,
                            event="files_ready",
                            message=f"{len(saved_paths)} file(s) written — review before QA continues",
                            agent="developer",
                            phase="CONSTRUCTION",
                            payload={
                                "project_dir": str(project_dir_early),
                                "generated_files": saved_paths,
                                "iteration": final_state.get("iteration", 0),
                            },
                        )

                    # After stripper: save exercise files to disk + emit event
                    if node_name == "stripper":
                        exercise_files = node_update.get("exercise_files", [])
                        objectives = node_update.get("learning_objectives", [])

                        # Save exercise files under runs/{run_id}/exercise/
                        exercise_dir = (run_output_dir / "exercise").resolve()
                        exercise_dir.mkdir(parents=True, exist_ok=True)
                        exercise_paths: list[str] = []
                        for ef in exercise_files:
                            p = ef["path"] if isinstance(ef, dict) else ef.path
                            c = ef["content"] if isinstance(ef, dict) else ef.content
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
                            c = gf["content"] if isinstance(gf, dict) else gf.content
                            if p:
                                dest = golden_dir / p
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_text(c, encoding="utf-8")

                        serialized = [
                            f if isinstance(f, dict) else f.model_dump()
                            for f in exercise_files
                        ]
                        await self._emit(
                            run_id=run_id,
                            event="exercise_ready",
                            message=(
                                f"Learning exercise ready — "
                                f"{len(objectives)} objective(s) to implement."
                            ),
                            agent="stripper",
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

            # Save all artifacts
            output_dir = str(run_output_dir / "output")
            save_artifacts(final_state, output_dir)

            project_dir = (run_output_dir / "output" / "project").resolve()
            generated_files: list[str] = []
            for code_file in final_state.get("code_files", []):
                rel_path = (
                    code_file.get("path", "")
                    if isinstance(code_file, dict)
                    else code_file.path
                )
                if rel_path:
                    generated_files.append(
                        str((project_dir / rel_path).resolve())
                    )

            result = {
                "qa_passed": final_state.get("qa_passed", False),
                "qa_feedback": final_state.get("qa_feedback", ""),
                "reasoning_logs": final_state.get("reasoning_logs", []),
                "output_dir": str((run_output_dir / "output").resolve()),
                "project_dir": str(project_dir),
                "generated_files": generated_files,
                "state": json.loads(json.dumps(final_state, default=_pydantic_encoder)),
            }

            self.store.set_result(run_id, result)

            # Learning mode: pause and wait for junior's submission
            if final_state.get("learning_mode") and final_state.get("exercise_files"):
                self.store.update_status(run_id, RunStatus.AWAITING_SUBMISSION)
                await self._emit(
                    run_id,
                    "awaiting_submission",
                    "Pipeline complete — waiting for your implementation.",
                    payload={
                        "learning_objectives": final_state.get(
                            "learning_objectives", []
                        ),
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

        except Exception as exc:
            error_msg = f"{exc!s}\n{traceback.format_exc()}"
            print(f"[ERROR] Run {run_id} failed: {error_msg}", file=sys.stderr)
            self.store.set_error(run_id, str(exc))
            await self._emit(
                run_id,
                "run_failed",
                f"Build failed: {exc!s}",
                payload={"error": error_msg},
            )
        finally:
            if not graph_task.done():
                graph_task.cancel()
