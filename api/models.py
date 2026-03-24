"""API request/response models for the JAME orchestrator."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    """Lifecycle states for a pipeline run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_SUBMISSION = "awaiting_submission"
    AWAITING_SPECS_REVIEW = "awaiting_specs_review"


class RunCreateRequest(BaseModel):
    """Request body for POST /runs."""

    user_request: str = Field(
        min_length=5,
        description="High-level description of the application to build.",
    )
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum QA→Developer retry loops before forcing completion.",
    )
    learning_mode: bool = Field(
        default=False,
        description="Enable Learning Mode: generates exercise files with TODOs for junior developers.",
    )
    mode: str = Field(
        default="senior",
        pattern="^(junior|senior|expert)$",
        description="Agent behaviour mode: junior (minimal), senior (production), expert (enterprise).",
    )


class ClarifyRequest(BaseModel):
    """Request body for POST /runs/{run_id}/clarify."""

    answer: str = Field(
        min_length=1,
        description="User's answer to the architect's clarification question.",
    )


class SpecsReviewRequest(BaseModel):
    """Request body for POST /runs/{run_id}/approve-specs."""

    action: str = Field(
        pattern="^(approve|revise)$",
        description="'approve' to proceed to code generation, 'revise' to provide feedback.",
    )
    feedback: str = Field(
        default="",
        description="Revision feedback when action='revise'. Ignored when action='approve'.",
    )


class QueuePromptRequest(BaseModel):
    """Request body for POST /runs/{run_id}/queue-prompt (senior mode instruction injection)."""

    prompt: str = Field(
        min_length=1,
        description="Instruction to inject into the senior developer's next iteration.",
    )


class ToolResponseRequest(BaseModel):
    """Request body for POST /runs/{run_id}/tool-response."""

    tool_call_id: str = Field(
        description="The id of the tool_call event the user is responding to.",
    )
    action: str = Field(
        pattern="^(run|skip)$",
        description="'run' to execute the tool, 'skip' to skip it.",
    )


class SubmitRequest(BaseModel):
    """Request body for POST /runs/{run_id}/submit."""

    files: list[dict[str, Any]] = Field(
        description="List of submitted files: [{path, content, language}]"
    )


class SubmitResponse(BaseModel):
    """Response body for POST /runs/{run_id}/submit."""

    passed: bool
    score: int
    feedback: str
    per_objective: list[dict[str, Any]] = Field(default_factory=list)
    hint: str | None = None
    hint_level: int = 0


class ExerciseResponse(BaseModel):
    """Response body for GET /runs/{run_id}/exercise."""

    run_id: str
    exercise_files: list[dict[str, Any]]
    learning_objectives: list[str]
    hints_available: int


class RunCreateResponse(BaseModel):
    """Response body for POST /runs."""

    run_id: str
    status: RunStatus


class ReasoningEvent(BaseModel):
    """A single streaming event emitted over WebSocket during a run."""

    run_id: str
    event: str
    agent: str | None = None
    phase: str | None = None
    message: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    """Internal run record stored in memory."""

    run_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    request: RunCreateRequest
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RunStatusResponse(BaseModel):
    """Response body for GET /runs/{run_id}."""

    run_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    reasoning_logs: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
