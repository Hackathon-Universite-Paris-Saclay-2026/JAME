"""Shared state definition for the Multi-Agent Software Factory.

The AgentState flows through the LangGraph pipeline. Each agent reads
from and writes to specific keys, and appends its reasoning trace so
the orchestrator can expose a full Plan / Act / Reason log.
"""

from __future__ import annotations

import operator
import re
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field, field_validator


class ReasoningEntry(TypedDict):
    agent: str
    phase: str          # "plan" | "act" | "reason"
    content: str


def _sanitize_path(path: str) -> str:
    """Strip markdown artefacts, backticks, and leading/trailing junk from a file path."""
    # Remove markdown heading markers, bold/italic, backticks
    path = re.sub(r'[#*`]', '', path)
    # Remove numbering prefixes like "1. " or "- "
    path = re.sub(r'^\s*[\d]+\.\s*', '', path)
    path = re.sub(r'^\s*[-•]\s*', '', path)
    # Collapse whitespace, strip
    path = path.strip().strip('"').strip("'")
    # Remove any leading ./
    path = re.sub(r'^\./', '', path)
    # Remove characters illegal in file paths
    path = re.sub(r'[<>"|?*]', '', path)
    return path


class CodeFile(BaseModel):
    """A single generated source file."""
    path: str = Field(description="Relative file path, e.g. 'backend/main.py'")
    content: str = Field(description="Complete file content")
    language: str = Field(default="python", description="Programming language")

    @field_validator("path")
    @classmethod
    def clean_path(cls, v: str) -> str:
        return _sanitize_path(v)


class SingleFileContent(BaseModel):
    """Structured output schema for generating a single file's content."""
    content: str = Field(description="The complete, runnable file content. No markdown fences.")


class FilePlan(BaseModel):
    """Structured output schema for the file-planning step."""
    files: list[str] = Field(
        description="List of relative file paths to generate, e.g. "
                    "['backend/main.py', 'backend/models.py', 'tests/test_main.py']"
    )


class QAIssue(BaseModel):
    """A single actionable QA issue."""
    file: str = Field(description="File path the issue relates to, or 'GENERAL'")
    severity: str = Field(description="'critical' | 'major' | 'minor'")
    description: str = Field(description="What is wrong and how to fix it")


class QAResult(BaseModel):
    """Structured output schema for the QA agent."""
    passed: bool = Field(description="True if all checks pass, False otherwise")
    issues: list[QAIssue] = Field(
        default_factory=list,
        description="List of issues found. Empty if passed=True.",
    )


# Keep GeneratedCode for backward compat (unused in the new chunked path
# but referenced in imports elsewhere).
class GeneratedCode(BaseModel):
    """Structured output schema for the Developer agent (legacy)."""
    files: list[CodeFile] = Field(
        description="List of all generated source files."
    )


class AgentState(TypedDict):
    """Central state passed between all agents in the graph."""

    # ── User input ──────────────────────────────────────────────
    user_request: str

    # ── Architect outputs ───────────────────────────────────────
    specs: str                  # structured specification (modules, routes, models)
    diagrams: str               # Mermaid C4 diagrams

    # ── Developer outputs ───────────────────────────────────────
    code_files: list[CodeFile]  # generated source files

    # ── DevOps outputs ──────────────────────────────────────────
    cicd_yaml: str              # GitHub Actions workflow
    dockerfile: str             # Dockerfile

    # ── QA outputs ──────────────────────────────────────────────
    qa_passed: bool
    qa_feedback: str            # feedback sent back to Developer on failure
    qa_issues: list[QAIssue]    # structured issue tickets

    # ── Orchestrator bookkeeping ────────────────────────────────
    iteration: int                                                  # QA retry counter
    max_iterations: int                                             # safety cap
    reasoning_logs: Annotated[list[ReasoningEntry], operator.add]   # append-only trace
