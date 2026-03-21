"""DevOps Agent — Generates CI/CD pipelines and Docker configuration.

Responsibilities:
  1. Decide whether the project needs CI, CD, or both.
  2. Produce a GitHub Actions workflow (CI).
  3. Produce a Dockerfile, docker-compose.yml, and .dockerignore (CD — services only).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from state import AgentState

# ── Prompts (loaded from prompts/devops.yaml) ────────────────────────────────

_PROMPTS_FILE = Path(__file__).parent.parent / "prompts" / "devops.yaml"


def _load_prompts() -> dict:
    with open(_PROMPTS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


_PROMPTS = _load_prompts()
_ACTIONS = _PROMPTS["actions"]  # pinned SHAs — edit prompts/devops.yaml to update


# ── Decision model ───────────────────────────────────────────────────────────

class DevOpsDecision(BaseModel):
    needs_ci: bool = Field(
        description=(
            "True if the project needs a CI pipeline. "
            "True for any project with tests, dependencies, or multiple files."
        )
    )
    needs_cd: bool = Field(
        description=(
            "True if the project needs containerized deployment artifacts "
            "(Dockerfile, docker-compose, .dockerignore). "
            "True only when the project exposes a network service: API, web app, worker, daemon. "
            "False for pure libraries, utility functions, or scripts with no server."
        )
    )
    reasoning: str = Field(description="One-sentence justification for this decision.")


_DECISION_PROMPT = _PROMPTS["decision"]["classify"]

_sha = dict(
    checkout=_ACTIONS["checkout"],
    setup_python=_ACTIONS["setup_python"],
    cache=_ACTIONS["cache"],
)
_shared = _PROMPTS["act"]["shared"]

_CI_PROMPT   = (_PROMPTS["act"]["ci_only"] + _shared).format(**_sha)
_FULL_PROMPT = (_PROMPTS["act"]["full"]    + _shared).format(**_sha)


# ── Extraction helper ────────────────────────────────────────────────────────

def _extract_block(raw: str, start_marker: str, end_marker: str) -> str:
    """Extract content between markers, stripping optional code fences."""
    if start_marker not in raw or end_marker not in raw:
        return ""
    block = raw.split(start_marker)[1].split(end_marker)[0].strip()
    for fence in ("```yaml", "```dockerfile", "```"):
        if block.startswith(fence):
            block = block[len(fence):]
            break
    if block.rstrip().endswith("```"):
        block = block.rstrip()[:-3].rstrip()
    return block.strip()


# ── Decision helper ──────────────────────────────────────────────────────────

def _decide(llm: ChatOpenAI, context: str) -> DevOpsDecision:
    """Ask the LLM whether CI and/or CD artifacts are needed."""
    try:
        structured = llm.with_structured_output(DevOpsDecision)
        return structured.invoke([
            SystemMessage(content=_DECISION_PROMPT),
            HumanMessage(content=context),
        ])
    except Exception:
        return DevOpsDecision(
            needs_ci=True,
            needs_cd=True,
            reasoning="Fallback: structured output unavailable — generating full CI+CD.",
        )


# ── Main node ────────────────────────────────────────────────────────────────

def devops_node(state: AgentState) -> dict:
    """LangGraph node: run the DevOps agent."""

    print("\n" + "=" * 60)
    print("⚙️  DEVOPS AGENT — Generating CI/CD & Docker")
    print("=" * 60)

    llm = ChatOpenAI(
        model="llama3.1-70b",
        temperature=0.1,
        max_tokens=4096,
        openai_api_key=os.getenv("SNOWFLAKE_API_KEY"),
        openai_api_base=os.getenv("SNOWFLAKE_API_BASE"),
    )

    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    file_list = (
        "\n".join(f"- {f['path']} ({f['language']})" for f in code_files)
        or "(none yet)"
    )

    context = (
        f"## Application Specifications\n{specs}\n\n"
        f"## Generated Source Files\n{file_list}"
    )

    # ── Plan phase: decide CI vs CI+CD ──────────────────────────────────────
    print("\n[PLAN] Deciding CI/CD scope …")
    decision = _decide(llm, context)
    plan_trace = f"CI={decision.needs_ci}, CD={decision.needs_cd}. {decision.reasoning}"
    print(f"[PLAN] {plan_trace}")

    if not decision.needs_ci:
        print("[SKIP] No CI/CD artifacts needed for this project type.\n")
        return {
            "cicd_yaml":           "",
            "dockerfile":          "",
            "docker_compose_yaml": "",
            "dockerignore":        "",
            "needs_cd":            False,
            "reasoning_logs": [
                {"agent": "devops", "phase": "plan",   "content": plan_trace},
                {"agent": "devops", "phase": "act",    "content": "Skipped — no CI/CD required."},
                {"agent": "devops", "phase": "reason", "content": "Project is a pure library or script with no service."},
            ],
        }

    # ── Act phase ────────────────────────────────────────────────────────────
    system_prompt = _FULL_PROMPT if decision.needs_cd else _CI_PROMPT
    mode = "CI + CD" if decision.needs_cd else "CI only"
    print(f"[ACT]  Calling LLM ({mode}) …")

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=context),
    ])
    raw = response.content

    cicd_yaml        = _extract_block(raw, "===CICD_START===",            "===CICD_END===")
    dockerfile       = _extract_block(raw, "===DOCKERFILE_START===",      "===DOCKERFILE_END===")      if decision.needs_cd else ""
    docker_compose   = _extract_block(raw, "===COMPOSE_START===",         "===COMPOSE_END===")         if decision.needs_cd else ""
    dockerignore     = _extract_block(raw, "===DOCKERIGNORE_START===",    "===DOCKERIGNORE_END===")    if decision.needs_cd else ""
    requirements     = _extract_block(raw, "===REQUIREMENTS_START===",    "===REQUIREMENTS_END===")
    requirements_dev = _extract_block(raw, "===REQUIREMENTS_DEV_START===","===REQUIREMENTS_DEV_END===")
    pyproject_toml   = _extract_block(raw, "===PYPROJECT_START===",       "===PYPROJECT_END===")
    makefile         = _extract_block(raw, "===MAKEFILE_START===",        "===MAKEFILE_END===")

    # ── Reason phase ─────────────────────────────────────────────────────────
    parts = [f"CI/CD YAML: {len(cicd_yaml)} chars"]
    if decision.needs_cd:
        parts += [
            f"Dockerfile: {len(dockerfile)} chars",
            f"docker-compose: {len(docker_compose)} chars",
            f".dockerignore: {len(dockerignore)} chars",
        ]
    parts += [
        f"requirements.txt: {len(requirements)} chars",
        f"requirements-dev.txt: {len(requirements_dev)} chars",
        f"pyproject.toml: {len(pyproject_toml)} chars",
        f"Makefile: {len(makefile)} chars",
    ]
    reason_trace = ", ".join(parts) + "."
    print(f"[REASON] {reason_trace}\n")

    return {
        "cicd_yaml":           cicd_yaml,
        "dockerfile":          dockerfile,
        "docker_compose_yaml": docker_compose,
        "dockerignore":        dockerignore,
        "requirements":        requirements,
        "requirements_dev":    requirements_dev,
        "pyproject_toml":      pyproject_toml,
        "makefile":            makefile,
        "needs_cd":            decision.needs_cd,
        "reasoning_logs": [
            {"agent": "devops", "phase": "plan",   "content": plan_trace},
            {"agent": "devops", "phase": "act",    "content": f"Generated {mode} artifacts."},
            {"agent": "devops", "phase": "reason", "content": reason_trace},
        ],
    }
