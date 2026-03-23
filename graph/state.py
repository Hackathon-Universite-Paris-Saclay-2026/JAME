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


class ReasoningEntry(TypedDict, total=False):
    """A single entry in the agent reasoning trace."""

    agent: str
    phase: str  # "plan" | "act" | "reason"
    content: str
    thinking: (
        str  # optional LLM reasoning block (shown collapsible in VS Code UI)
    )


def _sanitize_path(path: str) -> str:
    """Strip markdown artefacts, backticks, and leading/trailing junk from a file path.

    Args:
        path: Raw path string potentially containing LLM formatting noise.

    Returns:
        A clean, usable relative file path.
    """
    path = re.sub(r"[#*`]", "", path)
    path = re.sub(r"^\s*\d+\.\s*", "", path)
    path = re.sub(r"^\s*[-•]\s*", "", path)
    path = path.strip().strip('"').strip("'")
    path = re.sub(r"^\./", "", path)
    return re.sub(r'[<>"|?*]', "", path)


class CodeFile(BaseModel):
    """A single generated source file."""

    path: str = Field(description="Relative file path, e.g. 'backend/main.py'")
    content: str = Field(description="Complete file content")
    language: str = Field(default="python", description="Programming language")

    @field_validator("path")
    @classmethod
    def clean_path(cls, v: str) -> str:
        """Sanitize the path on construction."""
        return _sanitize_path(v)


class SingleFileContent(BaseModel):
    """Structured output schema for generating a single file's content."""

    content: str = Field(
        description="The complete, runnable file content. No markdown fences."
    )


class FilePlan(BaseModel):
    """Structured output schema for the file-planning step."""

    files: list[str] = Field(
        description=(
            "List of relative file paths to generate, e.g. "
            "['backend/main.py', 'backend/models.py', 'tests/test_main.py']"
        )
    )


class FunctionalDesign(BaseModel):
    """Structured output from the AIDLC-inspired functional design phase.

    Contains the technology-agnostic analysis of what the application does,
    extracted before file planning and code generation.
    """

    content: str = Field(
        description=(
            "The full functional design analysis including domain entities, "
            "business rules, API endpoints, component dependency map, and "
            "NFR considerations."
        )
    )


class ValidationIssue(BaseModel):
    """A single consistency issue found during self-validation."""

    file: str = Field(description="File path the issue relates to")
    severity: str = Field(description="'critical' | 'major' | 'minor'")
    description: str = Field(description="What is wrong and how to fix it")


class ValidationResult(BaseModel):
    """Structured output from the AIDLC-inspired self-validation phase."""

    passed: bool = Field(
        description="True if all consistency checks pass, False otherwise"
    )
    issues: list[ValidationIssue] = Field(
        default_factory=list,
        description="List of consistency issues found. Empty if passed=True.",
    )


class File(BaseModel):
    """A generic generated file (CI or CD artifact)."""

    path: str = Field(
        description="Relative file path, e.g. '.github/workflows/ci.yml'"
    )
    content: str = Field(description="Complete file content")

    @field_validator("path")
    @classmethod
    def clean_path(cls, v: str) -> str:
        """Sanitize the path on construction."""
        return _sanitize_path(v)


class QAIssue(BaseModel):
    """A single actionable QA issue."""

    file: str = Field(
        description="File path the issue relates to, or 'GENERAL'"
    )
    severity: str = Field(description="'critical' | 'major' | 'minor'")
    description: str = Field(description="What is wrong and how to fix it")


class QAResult(BaseModel):
    """Structured output schema for the QA agent."""

    passed: bool = Field(description="True if all checks pass, False otherwise")
    issues: list[QAIssue] = Field(
        default_factory=list,
        description="List of issues found. Empty if passed=True.",
    )


# Kept for backward compatibility with any existing serialised state.
class GeneratedCode(BaseModel):
    """Structured output schema for the Developer agent (legacy — chunked path replaces this)."""

    files: list[CodeFile] = Field(
        description="List of all generated source files."
    )


class AgentState(TypedDict):
    """Central state passed between all agents in the LangGraph pipeline."""

    # ── User input ──────────────────────────────────────────────
    user_request: str

    # ── Architect outputs ──────────────────────────────────
    scope: str  # "function" | "feature" | "system" | "product"
    specs: str  # structured specification (modules, routes, models)
    diagrams: str  # Mermaid C4 diagrams

    # ── Developer outputs ───────────────────────────────────────
    functional_design: str  # AIDLC-inspired functional design analysis
    code_files: list[CodeFile]  # generated source files

    # ── DevOps outputs ──────────────────────────────────────────
    ci_files: list[
        File
    ]  # CI artifacts (.github/workflows, Makefile, requirements*, pyproject.toml, .gitignore)
    cd_files: list[
        File
    ]  # CD artifacts (Dockerfile, docker-compose.yml, .dockerignore, .env.example)
    needs_ci: bool  # whether CI artifacts were generated
    needs_cd: bool  # whether CD artifacts were generated

    # ── QA outputs ──────────────────────────────────────────────
    qa_passed: bool
    qa_feedback: str  # plain-text feedback sent back to Developer on failure
    qa_issues: list[QAIssue]  # structured, actionable issue tickets

    # ── Orchestrator bookkeeping ────────────────────────────────
    iteration: int  # QA retry counter
    max_iterations: int  # safety cap
    reasoning_logs: Annotated[
        list[ReasoningEntry], operator.add
    ]  # append-only trace

    # ── Learning mode ────────────────────────────────────────────
    learning_mode: bool  # True → run Stripper after DevOps
    golden_files: list[CodeFile]  # complete solution (hidden from junior)
    exercise_files: list[CodeFile]  # files with TODOs for the junior
    learning_objectives: list[str]  # what the junior must implement
    hints: list[str]  # progressive hints (unlocked one by one)
    validation_feedback: str  # feedback from Validator after submission
    hint_level: int  # number of hints unlocked so far
