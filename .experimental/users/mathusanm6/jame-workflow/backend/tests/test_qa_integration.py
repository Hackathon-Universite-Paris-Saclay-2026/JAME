"""Integration tests for the Quality Engineer agent.

Real LLM calls (Snowflake Cortex / deepseek-r1) — requires a valid .env.
Mock code files are planted with deliberate bugs so the QA agent has something
concrete to find and report.

Run (all tests):
    pytest tests/test_qa_integration.py -v -s

Run (fast smoke test only — 1 LLM call):
    pytest tests/test_qa_integration.py -v -s -m fast

Each test feeds crafted source files into quality_engineer_node() and asserts
on the real QA verdict: severity, security rules cited, pass/fail decision.

Progress is printed to stderr so it is visible even without -s.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from dotenv import load_dotenv

load_dotenv()

from state import AgentState, CodeFile
from agents.quality_engineer import quality_engineer_node

# ── Skip guard ────────────────────────────────────────────────────────────────

if not os.getenv("SNOWFLAKE_API_KEY"):
    pytest.skip(
        "SNOWFLAKE_API_KEY not set — skipping integration tests.",
        allow_module_level=True,
    )


# ── Progress helper ───────────────────────────────────────────────────────────

def _progress(msg: str) -> None:
    """Print a timestamped progress line to stderr (visible without -s)."""
    print(f"\n[QA-TEST] {msg}", file=sys.stderr, flush=True)

# ── Shared specs ──────────────────────────────────────────────────────────────

SPECS = """\
## Task Manager REST API
Routes:
  - POST   /tasks          Create task (title: str, priority: low|medium|high)  [AUTH]
  - GET    /tasks          List all tasks                                        [AUTH]
  - GET    /tasks/{id}     Get task by ID                                        [AUTH]
  - DELETE /tasks/{id}     Delete task                                           [AUTH]

Security contracts:
  - All routes require a valid JWT bearer token (SECURITY-08)
  - Secrets must never be hardcoded (SECURITY-12)
  - All inputs must be validated (SECURITY-05)
  - Errors must return generic messages, never stack traces (SECURITY-15)

Data model:
  Task: id (int), title (str, non-empty), priority (enum), created_at (datetime)
"""


def _state(files: list[CodeFile], iteration: int = 0, max_iterations: int = 1) -> AgentState:
    return {
        "user_request":   "Build a task manager",
        "specs":          SPECS,
        "diagrams":       "",
        "code_files":     files,
        "cicd_yaml":      "",
        "dockerfile":     "",
        "qa_passed":      False,
        "qa_feedback":    "",
        "qa_issues":      [],
        "iteration":      iteration,
        "max_iterations": max_iterations,
        "reasoning_logs": [],
    }


# ── Sample files ──────────────────────────────────────────────────────────────

# A correct, minimal FastAPI task router — should PASS
CLEAN_ROUTER = CodeFile(
    path="backend/routers/tasks.py",
    language="python",
    content="""\
\"\"\"Task router — delegates all logic to the service layer.\"\"\"
from fastapi import APIRouter, Depends, HTTPException, status
from backend.services.task_service import TaskService
from backend.models import TaskCreate, TaskResponse
from backend.auth import get_current_user

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    current_user=Depends(get_current_user),
    svc: TaskService = Depends(),
):
    \"\"\"Create a new task. Requires authentication.\"\"\"
    return svc.create(payload, owner=current_user)


@router.get("/", response_model=list[TaskResponse])
def list_tasks(
    current_user=Depends(get_current_user),
    svc: TaskService = Depends(),
):
    \"\"\"List all tasks for the current user.\"\"\"
    return svc.list_all(owner=current_user)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    current_user=Depends(get_current_user),
    svc: TaskService = Depends(),
):
    \"\"\"Get a single task by ID. Raises 404 if not found.\"\"\"
    task = svc.get(task_id, owner=current_user)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: int,
    current_user=Depends(get_current_user),
    svc: TaskService = Depends(),
):
    \"\"\"Delete a task by ID. Raises 404 if not found.\"\"\"
    svc.delete(task_id, owner=current_user)
""",
)

# Bug 1: hardcoded secret (SECURITY-12)
FILE_HARDCODED_SECRET = CodeFile(
    path="backend/auth.py",
    language="python",
    content="""\
\"\"\"Authentication utilities.\"\"\"
import jwt

SECRET_KEY = "super_secret_password_123"   # hardcoded — never do this
ALGORITHM  = "HS256"


def create_token(user_id: int) -> str:
    return jwt.encode({"sub": str(user_id)}, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str) -> int:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    return int(payload["sub"])
""",
)

# Bug 2: missing auth on routes (SECURITY-08) + SQL injection risk (SECURITY-05)
FILE_MISSING_AUTH = CodeFile(
    path="backend/routers/tasks_insecure.py",
    language="python",
    content="""\
\"\"\"Insecure task router — no authentication, raw SQL.\"\"\"
from fastapi import APIRouter
from backend.database import engine

router = APIRouter(prefix="/tasks")


@router.get("/")
def list_tasks():
    # No auth check — anyone can list all tasks
    with engine.connect() as conn:
        # SQL injection: user input concatenated directly
        results = conn.execute("SELECT * FROM tasks WHERE owner = " + "1")
        return results.fetchall()


@router.delete("/{task_id}")
def delete_task(task_id: int):
    # No ownership check — any user can delete any task
    with engine.connect() as conn:
        conn.execute(f"DELETE FROM tasks WHERE id = {task_id}")
""",
)

# Bug 3: error leaking stack trace (SECURITY-15) + no input validation (SECURITY-05)
FILE_LEAKING_ERRORS = CodeFile(
    path="backend/services/task_service.py",
    language="python",
    content="""\
\"\"\"Task service — leaks internal errors to the client.\"\"\"
import traceback
from fastapi import HTTPException


class TaskService:
    def create(self, payload, owner):
        try:
            # No validation: title could be empty string
            task = {"title": payload.title, "owner": owner}
            return task
        except Exception as e:
            # Leaks full stack trace in HTTP response — never do this
            raise HTTPException(status_code=500, detail=traceback.format_exc())

    def get(self, task_id, owner):
        try:
            return None
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
""",
)

# Bug 4: completely empty stub file
FILE_EMPTY_STUB = CodeFile(
    path="backend/models.py",
    language="python",
    content="""\
# TODO: implement models
pass
""",
)

# A correct models file — no issues
CLEAN_MODELS = CodeFile(
    path="backend/models.py",
    language="python",
    content="""\
\"\"\"Pydantic schemas and SQLAlchemy ORM models for the Task Manager.\"\"\"
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import DeclarativeBase


class Priority(str, Enum):
    low    = "low"
    medium = "medium"
    high   = "high"


class Base(DeclarativeBase):
    pass


class TaskORM(Base):
    __tablename__ = "tasks"
    id         = Column(Integer, primary_key=True, index=True)
    title      = Column(String, nullable=False)
    priority   = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaskCreate(BaseModel):
    title:    str      = Field(..., min_length=1, description="Task title, non-empty")
    priority: Priority = Field(..., description="Task priority level")


class TaskResponse(BaseModel):
    id:         int
    title:      str
    priority:   Priority
    created_at: datetime

    model_config = {"from_attributes": True}
""",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _severities(result: dict) -> set[str]:
    return {i["severity"] for i in result["qa_issues"]}


def _files_with_issues(result: dict) -> set[str]:
    return {i["file"] for i in result["qa_issues"] if i["severity"] != "minor"}


def _security_rules_cited(result: dict) -> set[str]:
    """Extract SECURITY-XX references from issue descriptions."""
    import re
    rules = set()
    for issue in result["qa_issues"]:
        rules.update(re.findall(r"SECURITY-\d+", issue.get("description", "")))
    return rules


# ── Timed runner ─────────────────────────────────────────────────────────────

def _run_timed(label: str, files: list[CodeFile], **state_kwargs) -> dict:
    """Run quality_engineer_node with progress + elapsed time printed to stderr."""
    n = len(files)
    names = ", ".join(f.path.split("/")[-1] for f in files)
    _progress(f"START  {label}  [{n} file(s): {names}]  — making LLM calls, please wait...")
    t0 = time.monotonic()
    result = quality_engineer_node(_state(files, **state_kwargs))
    elapsed = time.monotonic() - t0
    verdict = "PASS" if result["qa_passed"] else "FAIL"
    issues  = len(result["qa_issues"])
    _progress(f"DONE   {label}  →  {verdict}  |  {issues} issue(s)  |  {elapsed:.1f}s")
    return result


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.fast
class TestSmoke:
    """Minimal fast smoke test — single file, fewest possible LLM calls.

    Run alone with:  pytest tests/test_qa_integration.py -v -s -m fast
    """

    def test_hardcoded_secret_detected(self):
        """One buggy file → QA FAIL with a critical issue. Fastest meaningful check."""
        result = _run_timed("smoke:hardcoded_secret", [FILE_HARDCODED_SECRET])

        assert result["qa_passed"] is False, (
            f"Expected FAIL for hardcoded secret.\nIssues: {result['qa_issues']}"
        )
        severities = {i["severity"] for i in result["qa_issues"]}
        assert "critical" in severities, (
            f"Expected at least one critical issue.\nIssues: {result['qa_issues']}"
        )


class TestCleanCode:
    def test_clean_router_and_models_pass(self):
        """Correct router + correct models → QA PASS."""
        result = _run_timed("clean:router+models", [CLEAN_ROUTER, CLEAN_MODELS])

        assert result["qa_passed"] is True, (
            f"Expected PASS but got FAIL.\nIssues: {result['qa_issues']}\n"
            f"Feedback:\n{result['qa_feedback']}"
        )
        # No critical or major issues
        blocking = [i for i in result["qa_issues"] if i["severity"] in ("critical", "major")]
        assert blocking == [], f"Unexpected blocking issues: {blocking}"


class TestHardcodedSecret:
    def test_detects_hardcoded_secret(self):
        """Hardcoded SECRET_KEY in auth.py → critical issue, SECURITY-12 cited."""
        result = _run_timed("secret:detect", [FILE_HARDCODED_SECRET])

        assert result["qa_passed"] is False
        assert "critical" in _severities(result), (
            f"Expected critical severity.\nIssues: {result['qa_issues']}"
        )
        assert "backend/auth.py" in _files_with_issues(result)

    def test_feedback_contains_fix_instructions(self):
        """Fix instructions for hardcoded secret must mention env var or os.environ."""
        result = _run_timed("secret:feedback", [FILE_HARDCODED_SECRET])

        feedback_lower = result["qa_feedback"].lower()
        assert any(kw in feedback_lower for kw in ("env", "environ", "secret", "os.")), (
            f"Expected fix instructions to mention env vars.\nFeedback:\n{result['qa_feedback']}"
        )


class TestMissingAuth:
    def test_detects_missing_auth_on_routes(self):
        """Routes with no authentication → critical/major issue, SECURITY-08."""
        result = _run_timed("auth:missing_auth", [FILE_MISSING_AUTH])

        assert result["qa_passed"] is False
        assert "critical" in _severities(result) or "major" in _severities(result)
        assert "backend/routers/tasks_insecure.py" in _files_with_issues(result)

    def test_detects_sql_injection(self):
        """Raw string SQL concatenation → critical issue."""
        result = _run_timed("auth:sql_injection", [FILE_MISSING_AUTH])

        descriptions = " ".join(i["description"] for i in result["qa_issues"]).lower()
        assert any(kw in descriptions for kw in ("sql", "inject", "concatenat", "parameteriz")), (
            f"Expected SQL injection mention.\nIssues: {result['qa_issues']}"
        )


class TestLeakingErrors:
    def test_detects_stack_trace_leak(self):
        """traceback.format_exc() in HTTP response → critical issue, SECURITY-15."""
        result = _run_timed("errors:stack_trace", [FILE_LEAKING_ERRORS])

        assert result["qa_passed"] is False
        descriptions = " ".join(i["description"] for i in result["qa_issues"]).lower()
        assert any(kw in descriptions for kw in ("stack", "trace", "traceback", "leak", "internal")), (
            f"Expected stack trace leak mention.\nIssues: {result['qa_issues']}"
        )

    def test_detects_missing_input_validation(self):
        """Empty title not validated → issue found."""
        result = _run_timed("errors:input_validation", [FILE_LEAKING_ERRORS])

        descriptions = " ".join(i["description"] for i in result["qa_issues"]).lower()
        assert any(kw in descriptions for kw in ("valid", "empty", "input", "title")), (
            f"Expected input validation mention.\nIssues: {result['qa_issues']}"
        )


class TestEmptyStub:
    def test_empty_stub_is_critical(self):
        """A models.py with only a TODO comment → critical issue."""
        result = _run_timed("stub:empty_model", [FILE_EMPTY_STUB])

        assert result["qa_passed"] is False
        assert "critical" in _severities(result), (
            f"Expected critical for empty stub.\nIssues: {result['qa_issues']}"
        )


class TestMultipleFilesWithMixedQuality:
    def test_clean_and_buggy_files_together(self):
        """One clean file + one buggy file → FAIL, only buggy file flagged."""
        result = _run_timed("mixed:clean+secret", [CLEAN_MODELS, FILE_HARDCODED_SECRET])

        assert result["qa_passed"] is False
        # The buggy auth file should be flagged
        assert "backend/auth.py" in _files_with_issues(result)

    def test_reasoning_logs_have_aidlc_structure(self):
        """All reasoning log entries carry AI-DLC phase/stage labels."""
        result = _run_timed("mixed:reasoning_logs", [CLEAN_MODELS])

        for entry in result["reasoning_logs"]:
            assert "phase" in entry,   f"Missing 'phase': {entry}"
            assert "stage" in entry,   f"Missing 'stage': {entry}"
            assert "agent" in entry,   f"Missing 'agent': {entry}"
            assert "content" in entry, f"Missing 'content': {entry}"
            assert entry["phase"].startswith("CONSTRUCTION/"), (
                f"Phase must follow 'CONSTRUCTION/<stage>' format: {entry['phase']}"
            )

    def test_all_critical_bugs_in_one_run(self):
        """Feed all four buggy files — QA must find issues across all of them."""
        all_buggy = [
            FILE_HARDCODED_SECRET,
            FILE_MISSING_AUTH,
            FILE_LEAKING_ERRORS,
            FILE_EMPTY_STUB,
        ]
        result = _run_timed("mixed:all_bugs", all_buggy)

        assert result["qa_passed"] is False
        # At least 3 different files flagged
        flagged = _files_with_issues(result)
        assert len(flagged) >= 3, (
            f"Expected ≥3 files flagged, got {len(flagged)}: {flagged}"
        )
        assert "critical" in _severities(result)
