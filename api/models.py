"""API request/response models for the JAME orchestrator."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
