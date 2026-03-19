"""DevOps Agent — Generates CI/CD pipelines and Docker configuration.

Responsibilities:
  1. Read the specs and the list of generated code files.
  2. Produce a GitHub Actions workflow YAML.
  3. Produce a Dockerfile for containerised deployment.
"""

from __future__ import annotations

import os

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from state import AgentState

SYSTEM_PROMPT = """\
You are the **DevOps Agent** of a multi-agent software factory.

## Role
Given the application specifications and a list of generated source files,
produce deployment and CI/CD artifacts.

## What to produce
Return your output in EXACTLY this format:

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

## GitHub Actions Workflow rules
- Trigger on push to `main` and on pull requests.
- Steps: checkout, setup Python, install dependencies, run linting, run tests.
- Use a matrix strategy for Python 3.11 and 3.12 if applicable.

## Dockerfile rules
- Use a slim Python base image.
- Copy only necessary files.
- Expose the correct port.
- Use a non-root user for security.

## Rules
- Produce ONLY the YAML and Dockerfile — no application code.
- Make the pipeline robust and production-ready.
"""


def _extract_block(raw: str, start_marker: str, end_marker: str) -> str:
    """Extract content between markers, stripping code fences."""
    if start_marker not in raw or end_marker not in raw:
        return ""
    block = raw.split(start_marker)[1].split(end_marker)[0].strip()
    # Strip code fences if present
    for fence in ("```yaml", "```dockerfile", "```"):
        if block.startswith(fence):
            block = block[len(fence):]
            break
    if block.rstrip().endswith("```"):
        block = block.rstrip()[:-3].rstrip()
    return block.strip()


def devops_node(state: AgentState) -> dict:
    """LangGraph node: run the DevOps agent."""

    print("\n" + "=" * 60)
    print("⚙️  DEVOPS AGENT — Generating CI/CD & Docker")
    print("=" * 60)

    llm = ChatOpenAI(
        model="deepseek-r1",
        temperature=0.1,
        max_tokens=4096,
        openai_api_key=os.getenv("SNOWFLAKE_API_KEY"),
        openai_api_base=os.getenv("SNOWFLAKE_API_BASE"),
    )

    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    file_list = "\n".join(f"- {f['path']} ({f['language']})" for f in code_files)

    # ── Plan phase ──────────────────────────────────────────────
    plan_trace = "Generating GitHub Actions CI/CD pipeline and Dockerfile."
    print(f"\n[PLAN] {plan_trace}")

    user_msg = (
        f"## Application Specifications\n{specs}\n\n"
        f"## Generated Source Files\n{file_list}"
    )

    # ── Act phase ───────────────────────────────────────────────
    print("[ACT]  Calling LLM to generate DevOps artifacts …")

    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ])

    raw = response.content

    cicd_yaml = _extract_block(raw, "===CICD_START===", "===CICD_END===")
    dockerfile = _extract_block(raw, "===DOCKERFILE_START===", "===DOCKERFILE_END===")

    # ── Reason phase ────────────────────────────────────────────
    reason_trace = (
        f"CI/CD YAML: {len(cicd_yaml)} chars, "
        f"Dockerfile: {len(dockerfile)} chars."
    )
    print(f"[REASON] {reason_trace}\n")

    return {
        "cicd_yaml": cicd_yaml,
        "dockerfile": dockerfile,
        "reasoning_logs": [
            {"agent": "devops", "phase": "plan", "content": plan_trace},
            {"agent": "devops", "phase": "act", "content": "Generated CI/CD and Dockerfile."},
            {"agent": "devops", "phase": "reason", "content": reason_trace},
        ],
    }
