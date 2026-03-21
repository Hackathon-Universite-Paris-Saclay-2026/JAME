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
    user_request: str = Field(min_length=5)
    max_iterations: int = Field(default=3, ge=1, le=10)


class RunCreateResponse(BaseModel):
    run_id: str
    status: RunStatus


class ReasoningEvent(BaseModel):
    run_id: str
    event: str
    agent: str | None = None
    phase: str | None = None
    message: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    request: RunCreateRequest
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RunStatusResponse(BaseModel):
    run_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    reasoning_logs: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
