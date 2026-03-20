"""DevOps Agent — Generates CI/CD pipelines and Docker configuration.

Responsibilities:
  1. Decide whether the project needs CI, CD, or both.
  2. Produce a GitHub Actions workflow (CI).
  3. Produce a Dockerfile, docker-compose.yml, and .dockerignore (CD — services only).
"""

from __future__ import annotations

import os

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from state import AgentState

# ── Pinned GitHub Actions SHAs ───────────────────────────────────────────────
# Verified 2025 — update when bumping action versions
_ACTIONS = {
    "checkout":     "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",      # v4.2.2
    "setup-python": "actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b",  # v5.3.0
    "cache":        "actions/cache@1bd1e32a3bdc45362d1e726936510720a7c6158d",          # v4.2.0
}


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


_DECISION_PROMPT = """\
You are the DevOps Agent of a multi-agent software factory.

Analyze the application specifications and source file list below.
Decide whether the project needs a CI pipeline and/or containerized CD artifacts.

Rules:
- needs_ci=true  -> project has tests, dependencies, or is multi-file
- needs_cd=true  -> project exposes a network service (API, web server, worker)
- needs_cd=false -> project is a pure library, utility function, or script with no server
"""


# ── CI-only system prompt ────────────────────────────────────────────────────

_CI_PROMPT = f"""\
You are the DevOps Agent of a multi-agent software factory.

Generate a GitHub Actions CI workflow for this project.
Return your output in EXACTLY this format (no other text):

===CICD_START===
```yaml
<GitHub Actions workflow YAML>
```
===CICD_END===

## CI Workflow requirements (mandatory)

### Triggers
- push to main
- pull_request to main

### Concurrency (cancel duplicate runs on the same branch)
```yaml
concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true
```

### Matrix
- Python versions: ["3.11", "3.12"]

### Steps (in this exact order)
1. uses: {_ACTIONS["checkout"]}
2. uses: {_ACTIONS["setup-python"]}
   with:
     python-version: ${{{{ matrix.python-version }}}}
3. uses: {_ACTIONS["cache"]}
   with:
     path: ~/.cache/pip
     key: ${{{{ runner.os }}}}-pip-${{{{ hashFiles('**/requirements*.txt') }}}}
     restore-keys: |
       ${{{{ runner.os }}}}-pip-
4. run: pip install -r requirements.txt
5. run: pip install ruff pytest pip-audit bandit
6. run: ruff check .
7. run: pytest --tb=short
8. run: pip-audit
9. run: bandit -r . -q

### Rules
- ALL action references MUST use the full commit SHA shown above — never @v3, @v4, or @main
- The workflow must be valid YAML that runs on GitHub Actions without modification
"""


# ── Full CI + CD system prompt ───────────────────────────────────────────────

_FULL_PROMPT = f"""\
You are the DevOps Agent of a multi-agent software factory.

Generate all CI/CD artifacts for this project.
Return your output in EXACTLY this format and order (no other text):

===CICD_START===
```yaml
<GitHub Actions workflow YAML>
```
===CICD_END===

===DOCKERFILE_START===
```dockerfile
<Dockerfile content>
```
===DOCKERFILE_END===

===COMPOSE_START===
```yaml
<docker-compose.yml content>
```
===COMPOSE_END===

===DOCKERIGNORE_START===
<.dockerignore content — plain text, no code fence>
===DOCKERIGNORE_END===

## CI Workflow requirements (mandatory)

### Triggers
- push to main
- pull_request to main

### Concurrency
```yaml
concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true
```

### Matrix
- Python versions: ["3.11", "3.12"]

### Steps (in this exact order)
1. uses: {_ACTIONS["checkout"]}
2. uses: {_ACTIONS["setup-python"]}
   with:
     python-version: ${{{{ matrix.python-version }}}}
3. uses: {_ACTIONS["cache"]}
   with:
     path: ~/.cache/pip
     key: ${{{{ runner.os }}}}-pip-${{{{ hashFiles('**/requirements*.txt') }}}}
     restore-keys: |
       ${{{{ runner.os }}}}-pip-
4. run: pip install -r requirements.txt
5. run: pip install ruff pytest pip-audit bandit
6. run: ruff check .
7. run: pytest --tb=short
8. run: pip-audit
9. run: bandit -r . -q

ALL action references MUST use the full commit SHA shown above — never @v3, @v4, or @main.

## Dockerfile requirements (mandatory)

- Multi-stage build: stage 1 (builder) installs all deps; stage 2 (final) copies only runtime
- Final stage base: python:3.12-slim
- Layer-cache order: COPY requirements.txt -> RUN pip install -> COPY source code
- Create a non-root user with useradd and switch with USER
- Add a HEALTHCHECK instruction
- EXPOSE the correct port derived from the specs
- CMD to start the application
- Never run as root, never hardcode credentials

## docker-compose.yml requirements

- Single service named after the application
- build: . (build from local Dockerfile)
- Port mapping matching the exposed port
- env_file: .env
- restart: unless-stopped

## .dockerignore requirements

Must exclude (one entry per line, no comments):
.env
venv/
__pycache__/
.git/
*.pyc
*.pyo
tests/
*.md
.mypy_cache/
dist/
build/
*.egg-info/
"""


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

    cicd_yaml      = _extract_block(raw, "===CICD_START===",         "===CICD_END===")
    dockerfile     = _extract_block(raw, "===DOCKERFILE_START===",   "===DOCKERFILE_END===")   if decision.needs_cd else ""
    docker_compose = _extract_block(raw, "===COMPOSE_START===",      "===COMPOSE_END===")      if decision.needs_cd else ""
    dockerignore   = _extract_block(raw, "===DOCKERIGNORE_START===", "===DOCKERIGNORE_END===") if decision.needs_cd else ""

    # ── Reason phase ─────────────────────────────────────────────────────────
    parts = [f"CI/CD YAML: {len(cicd_yaml)} chars"]
    if decision.needs_cd:
        parts += [
            f"Dockerfile: {len(dockerfile)} chars",
            f"docker-compose: {len(docker_compose)} chars",
            f".dockerignore: {len(dockerignore)} chars",
        ]
    reason_trace = ", ".join(parts) + "."
    print(f"[REASON] {reason_trace}\n")

    return {
        "cicd_yaml":           cicd_yaml,
        "dockerfile":          dockerfile,
        "docker_compose_yaml": docker_compose,
        "dockerignore":        dockerignore,
        "needs_cd":            decision.needs_cd,
        "reasoning_logs": [
            {"agent": "devops", "phase": "plan",   "content": plan_trace},
            {"agent": "devops", "phase": "act",    "content": f"Generated {mode} artifacts."},
            {"agent": "devops", "phase": "reason", "content": reason_trace},
        ],
    }
