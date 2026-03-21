from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cancel_token import CancelToken, RunCancelledError
from graph import build_graph
from main import save_artifacts
from state import AgentState

from .job_store import InMemoryRunStore
from .models import ReasoningEvent, RunCreateRequest, RunStatus


class OrchestratorService:
    def __init__(self, output_root: Path) -> None:
        self.store = InMemoryRunStore()
        self.output_root = output_root

    async def create_run(self, request: RunCreateRequest) -> str:
        run_id = str(uuid.uuid4())
        self.store.create_run(run_id, request)
        asyncio.create_task(self._run_pipeline(run_id, request))
        return run_id

    def cancel_run(self, run_id: str) -> bool:
        return self.store.cancel_run(run_id)

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
            timestamp=datetime.now(timezone.utc),
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

    async def _run_pipeline(self, run_id: str, request: RunCreateRequest) -> None:
        self.store.update_status(run_id, RunStatus.RUNNING)
        await self._emit(run_id, "run_started", "Starting orchestration...")

        token = CancelToken()
        self.store.register_token(run_id, token)

        state: AgentState = {
            "user_request": request.user_request,
            "specs": "",
            "diagrams": "",
            "code_files": [],
            "cicd_yaml": "",
            "dockerfile": "",
            "qa_passed": False,
            "qa_feedback": "",
            "qa_issues": [],
            "iteration": 0,
            "max_iterations": request.max_iterations,
            "reasoning_logs": [],
            "scope": "system",
            "_cancel_token": token,  # type: ignore[typeddict-unknown-key]
        }

        run_output_dir = self.output_root / run_id
        run_output_dir.mkdir(parents=True, exist_ok=True)

        previous_output = os.getenv("OUTPUT_DIR")
        final_state: AgentState = state

        # Queue for chunks produced by the sync graph thread
        chunk_queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run_graph_sync():
            """Run the LangGraph pipeline synchronously in a worker thread."""
            os.environ["OUTPUT_DIR"] = str(run_output_dir)
            graph = build_graph()
            try:
                for chunk in graph.stream(state, stream_mode="updates"):
                    loop.call_soon_threadsafe(chunk_queue.put_nowait, ("chunk", chunk))
            except RunCancelledError:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("cancelled", None))
            except Exception as exc:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("error", exc))
            else:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("done", None))

        try:
            # Run graph in thread — frees the event loop so WebSocket can stream
            graph_task = asyncio.create_task(
                asyncio.to_thread(_run_graph_sync)
            )

            while True:
                kind, payload = await chunk_queue.get()

                if kind == "cancelled":
                    await self._emit(run_id, "run_cancelled", "Build cancelled by user.")
                    self.store.update_status(run_id, RunStatus.CANCELLED)
                    return

                if kind == "error":
                    raise payload  # type: ignore[misc]

                if kind == "done":
                    break

                # kind == "chunk"
                chunk = payload
                for node_name, node_update in chunk.items():
                    if not isinstance(node_update, dict):
                        continue

                    final_state = self._merge_state(final_state, node_update)

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

                    # After developer: save code files immediately and emit files_ready
                    if node_name == "developer" and node_update.get("code_files"):
                        dev_files = node_update["code_files"]
                        # Write code files to disk right away (before QA / delivery)
                        project_dir_early = (run_output_dir / "output" / "project").resolve()
                        project_dir_early.mkdir(parents=True, exist_ok=True)
                        saved_paths: list[str] = []
                        for cf in dev_files:
                            p = cf["path"] if isinstance(cf, dict) else cf.path
                            c = cf["content"] if isinstance(cf, dict) else cf.content
                            lang = cf.get("language", "text") if isinstance(cf, dict) else cf.language
                            # Write to disk
                            from main import _sanitize_file_path
                            clean = _sanitize_file_path(p)
                            if clean:
                                dest = project_dir_early / clean
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_text(c, encoding="utf-8")
                                saved_paths.append(str(dest))
                            # Emit per-file event for UI preview
                            await self._emit(
                                run_id=run_id,
                                event="file_generated",
                                message=f"Generated: {p}",
                                agent="Developer",
                                phase="CONSTRUCTION",
                                payload={"path": p, "content": c, "language": lang},
                            )
                        # Emit files_ready so UI can show keep/undo immediately
                        await self._emit(
                            run_id=run_id,
                            event="files_ready",
                            message=f"{len(saved_paths)} file(s) written — review before QA continues",
                            agent="Developer",
                            phase="CONSTRUCTION",
                            payload={
                                "project_dir": str(project_dir_early),
                                "generated_files": saved_paths,
                                "iteration": final_state.get("iteration", 0),
                            },
                        )

            cwd = Path.cwd()
            os.chdir(run_output_dir)
            try:
                save_artifacts(final_state)
            finally:
                os.chdir(cwd)

            project_dir = (run_output_dir / "output" / "project").resolve()
            generated_files: list[str] = []
            for code_file in final_state.get("code_files", []):
                rel_path = code_file.get("path", "") if isinstance(code_file, dict) else code_file.path
                if rel_path:
                    generated_files.append(str((project_dir / rel_path).resolve()))

            result = {
                "qa_passed": final_state.get("qa_passed", False),
                "qa_feedback": final_state.get("qa_feedback", ""),
                "reasoning_logs": final_state.get("reasoning_logs", []),
                "output_dir": str((run_output_dir / "output").resolve()),
                "project_dir": str(project_dir),
                "generated_files": generated_files,
                "state": json.loads(json.dumps(final_state, default=str)),
            }

            self.store.set_result(run_id, result)
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
            import traceback

            error_msg = f"{str(exc)}\n{traceback.format_exc()}"
            print(f"[ERROR] Run {run_id} failed: {error_msg}", file=sys.stderr)
            self.store.set_error(run_id, str(exc))
            await self._emit(
                run_id,
                "run_failed",
                f"Build failed: {str(exc)}",
                payload={"error": error_msg},
            )
        finally:
            # Restore OUTPUT_DIR
            if previous_output is None:
                os.environ.pop("OUTPUT_DIR", None)
            else:
                os.environ["OUTPUT_DIR"] = previous_output
            # Ensure graph thread is not leaked
            if not graph_task.done():
                graph_task.cancel()
