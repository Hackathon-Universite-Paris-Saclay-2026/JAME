"""DevOps node — Generates CI/CD pipelines and Docker configuration.

Responsibilities:
  1. Read the specs and the list of generated code files.
  2. Produce a GitHub Actions workflow YAML.
  3. Produce a Dockerfile for containerised deployment.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from graph.prompts.devops_prompts import DEVOPS_SYSTEM_PROMPT
from graph.state import AgentState
from integrations.cortex import get_cortex_llm


def _extract_block(raw: str, start_marker: str, end_marker: str) -> str:
    """Extract content between markers, stripping code fences.

    Args:
        raw: Full LLM response string.
        start_marker: Opening delimiter string.
        end_marker: Closing delimiter string.

    Returns:
        The extracted block content, or an empty string if markers are absent.
    """
    if start_marker not in raw or end_marker not in raw:
        return ""
    block = raw.split(start_marker)[1].split(end_marker)[0].strip()
    for fence in ("```yaml", "```dockerfile", "```"):
        if block.startswith(fence):
            block = block[len(fence) :]
            break
    if block.rstrip().endswith("```"):
        block = block.rstrip()[:-3].rstrip()
    return block.strip()


def devops_node(state: AgentState) -> dict:
    """LangGraph node: run the DevOps agent.

    Args:
        state: Current pipeline state with ``specs`` and ``code_files``.

    Returns:
        A dict updating ``cicd_yaml``, ``dockerfile``, and ``reasoning_logs``.
    """
    print("\n" + "=" * 60)
    print("⚙️  DEVOPS AGENT — Generating CI/CD & Docker")
    print("=" * 60)

    llm = get_cortex_llm(model="deepseek-r1", temperature=0.1, max_tokens=4096)

    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    file_list = "\n".join(
        f"- {f['path']} ({f['language']})" for f in code_files
    )

    # ── Plan phase ──────────────────────────────────────────────
    plan_trace = "Generating GitHub Actions CI/CD pipeline and Dockerfile."
    print(f"\n[PLAN] {plan_trace}")

    user_msg = (
        f"## Application Specifications\n{specs}\n\n"
        f"## Generated Source Files\n{file_list}"
    )

    # ── Act phase ───────────────────────────────────────────────
    print("[ACT]  Calling LLM to generate DevOps artifacts …")

    response = llm.invoke(
        [
            SystemMessage(content=DEVOPS_SYSTEM_PROMPT),
            HumanMessage(content=user_msg),
        ]
    )

    raw = response.content
    cicd_yaml = _extract_block(raw, "===CICD_START===", "===CICD_END===")
    dockerfile = _extract_block(
        raw, "===DOCKERFILE_START===", "===DOCKERFILE_END==="
    )

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
            {
                "agent": "devops",
                "phase": "act",
                "content": "Generated CI/CD and Dockerfile.",
            },
            {"agent": "devops", "phase": "reason", "content": reason_trace},
        ],
    }
