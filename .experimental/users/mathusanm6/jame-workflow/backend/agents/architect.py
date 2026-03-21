"""Agent node implementations — Architect, Developer, Delivery Engineer."""

from __future__ import annotations

import re
import sys
from typing import Any

from cancel_token import CancelToken, RunCancelledError
from state import AgentState

try:
    from llm_provider import get_llm
    from prompt_manager import PromptManager
    llm = get_llm()
    pm = PromptManager()
    HAS_LLM = True
except Exception as e:
    print(f"[WARN] LLM not available: {e}", file=sys.stderr)
    HAS_LLM = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> tuple[str, str]:
    """Extract and strip <think>…</think> blocks from reasoning model output.

    Handles:
    - Well-formed  <think>…</think>  blocks
    - Unclosed     <think>…  (no closing tag — treat rest-of-string as thinking)
    - Multiple     <think> blocks (rare but possible)
    """
    if not text:
        return "", text

    thinking_parts: list[str] = []
    # Remove all well-formed <think>…</think> spans
    content = re.sub(
        r"<think>(.*?)</think>",
        lambda m: thinking_parts.append(m.group(1).strip()) or "",
        text,
        flags=re.DOTALL,
    )

    # Handle unclosed <think> — everything after the tag is reasoning
    if "<think>" in content:
        idx = content.index("<think>")
        thinking_parts.append(content[idx + len("<think>"):].strip())
        content = content[:idx]

    thinking = "\n\n".join(thinking_parts).strip()
    content = content.strip()

    # If stripping left nothing, the whole response was thinking — return
    # original so callers can still use it rather than getting empty string.
    if not content:
        content = text.strip()

    return thinking, content


def _call_llm(prompt: str, context: str = "", token: CancelToken | None = None) -> tuple[str, str]:
    """Call LLM, checking cancel token before and after. Returns (thinking, content)."""
    if token:
        token.raise_if_cancelled()
    full_prompt = prompt + ("\n\n" + context if context else "")
    response = llm.invoke(full_prompt)
    if token:
        token.raise_if_cancelled()
    return _strip_thinking(response.content)


# ── Architect Node ─────────────────────────────────────────────────────────────

def architect_node(state: AgentState) -> dict[str, Any]:
    """Architect: classify scope and produce context-appropriate specifications."""
    user_request = state.get("user_request", "")
    token: CancelToken | None = state.get("_cancel_token")  # type: ignore[assignment]

    print(f"\n=== ARCHITECT ===\nRequest: {user_request}")

    if not user_request:
        return {"specs": "No request provided", "diagrams": ""}

    logs: list[dict] = []
    thinking_blocks: list[str] = []

    try:
        if HAS_LLM:
            # Emit "working" immediately so the UI shows the agent started
            logs.append({
                "agent": "Architect",
                "phase": "INCEPTION",
                "stage": "scope",
                "content": "Classifying request scope...",
                "thinking": "",
            })

            # Step 1: classify scope
            classifier_prompt = pm.get_prompt("solution_architect", "analysis", "scope_classifier")
            if not classifier_prompt:
                classifier_prompt = (
                    "Classify the user's request into one of: "
                    "'function', 'feature', 'system', 'product'.\n"
                    "Return ONLY valid JSON: "
                    '{\"scope\": \"<scope>\", \"needs_interrogation\": false}\n\n'
                    "Request: {request}"
                )
            classifier_prompt = classifier_prompt.replace("{request}", user_request)

            thinking, scope_raw = _call_llm(classifier_prompt, token=token)
            if thinking:
                thinking_blocks.append(thinking)

            # Parse scope
            scope = "function"
            import json as _json
            scope_match = re.search(r'"scope"\s*:\s*"([^"]+)"', scope_raw)
            if scope_match:
                scope = scope_match.group(1).lower()

            print(f"[ARCHITECT] Scope classified as: {scope}")
            logs.append({
                "agent": "Architect",
                "phase": "INCEPTION",
                "stage": "scope",
                "content": f"Scope classified as: {scope}",
                "thinking": thinking,
            })

            # Step 2: generate specs appropriate to scope
            if scope in ("function", "feature"):
                # For simple scopes, produce minimal focused specs
                spec_prompt = (
                    "You are a software architect. The user wants a simple implementation.\n\n"
                    f"Request: {user_request}\n\n"
                    "Produce concise specifications:\n"
                    "1. What exactly to implement (function signature, behavior, edge cases)\n"
                    "2. Input/output contract\n"
                    "3. Language and style\n"
                    "4. Test cases to verify correctness\n\n"
                    "Be specific. No fluff. No full application structure needed."
                )
            else:
                design_prompt = pm.get_prompt("solution_architect", "design", "initial")
                if not design_prompt:
                    spec_prompt = (
                        f"You are a software architect. Design a full application.\n\n"
                        f"Request: {user_request}\n\n"
                        "Produce:\n"
                        "1. Module breakdown\n"
                        "2. API routes\n"
                        "3. Data models\n"
                        "4. Technology stack\n"
                        "5. C4 context diagram in ```mermaid block\n"
                    )
                else:
                    spec_prompt = design_prompt.replace(
                        "{memory_section}", ""
                    ) + f"\n\nUser request: {user_request}"

            thinking2, specs = _call_llm(spec_prompt, token=token)
            if thinking2:
                thinking_blocks.append(thinking2)

            diagrams = ""
            if "```mermaid" in specs:
                # Extract diagrams from specs
                mermaid_blocks = re.findall(r"```mermaid.*?```", specs, re.DOTALL)
                diagrams = "\n\n".join(mermaid_blocks)

            # Prepend scope hint so developer can use it
            specs = f"[SCOPE: {scope}]\n\n" + specs

            logs.append({
                "agent": "Architect",
                "phase": "INCEPTION",
                "stage": "design",
                "content": f"Specifications produced ({len(specs)} chars, scope: {scope})",
                "thinking": " | ".join(thinking_blocks) if thinking_blocks else "",
            })

        else:
            # Fallback: classify scope heuristically
            req_lower = user_request.lower()
            if any(w in req_lower for w in ("app", "api", "server", "service", "application", "website")):
                scope = "system"
            else:
                scope = "function"
            specs = (
                f"[SCOPE: {scope}]\n\n"
                f"## Specification\n\n**Request**: {user_request}\n\n"
                "Implement exactly what is requested. Focus on correctness.\n"
            )
            diagrams = ""
            logs.append({
                "agent": "Architect",
                "phase": "INCEPTION",
                "stage": "design",
                "content": f"Fallback specs generated (LLM unavailable)",
                "thinking": "",
            })

        print(f"[ARCHITECT] Returning {len(specs)} bytes of specs, scope={scope}")
        return {
            "specs": specs,
            "diagrams": diagrams,
            "scope": scope,
            "reasoning_logs": logs,
        }

    except Exception as e:
        error_msg = f"Architect error: {str(e)}"
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return {
            "specs": error_msg,
            "diagrams": "",
            "scope": "system",
            "reasoning_logs": [
                {"agent": "Architect", "phase": "INCEPTION", "stage": "error", "content": error_msg, "thinking": ""}
            ],
        }


# ── Developer Node ─────────────────────────────────────────────────────────────

def developer_node(state: AgentState) -> dict[str, Any]:
    """Developer: generate exactly what was requested, no more."""
    specs = state.get("specs", "")
    user_request = state.get("user_request", "")
    qa_feedback = state.get("qa_feedback", "")
    iteration = state.get("iteration", 0)
    token: CancelToken | None = state.get("_cancel_token")  # type: ignore[assignment]

    print(f"\n=== DEVELOPER === (iteration {iteration})")

    if not specs:
        specs = user_request

    logs: list[dict] = []

    try:
        code_files: list[dict] = []

        if HAS_LLM:
            logs.append({
                "agent": "Developer",
                "phase": "CONSTRUCTION",
                "stage": "generation",
                "content": "Generating code..." + (f" (revision {iteration})" if iteration > 0 else ""),
                "thinking": "",
            })
            generation_prompt = pm.get_prompt("software_engineer", "generate", "system")
            if not generation_prompt:
                generation_prompt = (
                    "You are a senior software engineer. Generate exactly what is requested.\n"
                    "Return ONLY file blocks in this exact format:\n"
                    "FILE: <relative_path>\n"
                    "CONTENT:\n"
                    "<raw file content>\n"
                    "---\n"
                    "No markdown fences around content. No explanations outside the blocks.\n"
                    "Generate ONLY the files needed — do not add boilerplate not asked for."
                )

            # Extract scope hint from specs if present
            scope_hint = "system"
            scope_match = re.search(r"\[SCOPE:\s*(\w+)\]", specs)
            if scope_match:
                scope_hint = scope_match.group(1)

            # Build context
            context_parts = [
                f"User request:\n{user_request}",
                f"\nScope: {scope_hint}",
                f"\nSpecifications:\n{specs[:6000]}",
            ]

            if qa_feedback:
                context_parts.append(
                    f"\nQA Feedback (fix these issues):\n{qa_feedback}"
                )
                if code_files:
                    context_parts.append(
                        "\nExisting files to fix (rewrite only what needs changing):"
                    )
                    for f in state.get("code_files", []):
                        p = f["path"] if isinstance(f, dict) else f.path
                        c = f["content"] if isinstance(f, dict) else f.content
                        context_parts.append(f"FILE: {p}\nCONTENT:\n{c[:2000]}\n---")

            scope_instructions = {
                "function": (
                    "SCOPE=function: Generate ONLY the minimal files for this specific function/algorithm. "
                    "Do NOT add FastAPI, React, databases, or any framework. "
                    "A single .py file is usually sufficient. Add a test file if requested."
                ),
                "feature": (
                    "SCOPE=feature: Generate only the files for this specific feature. "
                    "Do not scaffold a full app unless the specs require it."
                ),
                "system": (
                    "SCOPE=system: Generate all files for a complete application."
                ),
                "product": (
                    "SCOPE=product: Generate all files for a multi-service platform."
                ),
            }
            scope_note = scope_instructions.get(scope_hint, scope_instructions["system"])

            context_parts.append(
                f"\nIMPORTANT:\n"
                f"- {scope_note}\n"
                "- The FILE/CONTENT/--- format is mandatory for every file.\n"
                "- No markdown fences inside CONTENT blocks.\n"
                "- Code must be complete and runnable."
            )

            thinking, code_content = _call_llm(generation_prompt, "\n".join(context_parts), token=token)

            print(f"[DEVELOPER] LLM response received ({len(code_content)} chars)")

            # Parse FILE/CONTENT blocks
            block_pattern = re.compile(
                r"FILE:\s*(?P<path>[^\n]+)\nCONTENT:\n(?P<content>.*?)(?=\nFILE:|\n---\n|\Z)",
                re.DOTALL,
            )
            for match in block_pattern.finditer(code_content):
                file_path = match.group("path").strip()
                file_content = match.group("content").strip()
                if not file_path or not file_content:
                    continue
                # Detect language
                if file_path.endswith(".py"):
                    lang = "python"
                elif file_path.endswith((".js", ".jsx", ".ts", ".tsx")):
                    lang = "javascript"
                elif file_path.endswith((".yml", ".yaml")):
                    lang = "yaml"
                elif file_path.endswith(".json"):
                    lang = "json"
                else:
                    lang = "text"
                code_files.append({"path": file_path, "content": file_content, "language": lang})

            logs.append({
                "agent": "Developer",
                "phase": "CONSTRUCTION",
                "stage": "generation",
                "content": f"Generated {len(code_files)} file(s) via LLM",
                "thinking": thinking,
            })

            # Parser-repair retry: if the LLM produced content but we couldn't
            # parse FILE/CONTENT blocks, ask it to reformat the output.
            if not code_files and code_content.strip():
                print("[DEVELOPER] Parser-repair retry: asking LLM to reformat output.")
                logs.append({
                    "agent": "Developer",
                    "phase": "CONSTRUCTION",
                    "stage": "repair",
                    "content": "Reformatting output into FILE/CONTENT blocks...",
                    "thinking": "",
                })
                repair_prompt = (
                    "The following code was produced but is not in the required format.\n"
                    "Reformat it STRICTLY as FILE/CONTENT/--- blocks with NO other text.\n"
                    "Format:\n"
                    "FILE: <relative_path>\nCONTENT:\n<file content>\n---\n\n"
                    "Code to reformat:\n" + code_content[:8000]
                )
                _, repaired = _call_llm(repair_prompt, token=token)
                for match in block_pattern.finditer(repaired):
                    file_path = match.group("path").strip()
                    file_content = match.group("content").strip()
                    if not file_path or not file_content:
                        continue
                    if file_path.endswith(".py"):
                        lang = "python"
                    elif file_path.endswith((".js", ".jsx", ".ts", ".tsx")):
                        lang = "javascript"
                    elif file_path.endswith((".yml", ".yaml")):
                        lang = "yaml"
                    elif file_path.endswith(".json"):
                        lang = "json"
                    else:
                        lang = "text"
                    code_files.append({"path": file_path, "content": file_content, "language": lang})
                if code_files:
                    logs.append({
                        "agent": "Developer",
                        "phase": "CONSTRUCTION",
                        "stage": "repair",
                        "content": f"Parser-repair succeeded: {len(code_files)} file(s) recovered",
                        "thinking": "",
                    })

        # Fallback only if LLM completely failed to produce parseable output
        if not code_files:
            print("[DEVELOPER] No parseable output from LLM. Using smart fallback.")
            code_files = _generate_fallback(user_request, specs)
            logs.append({
                "agent": "Developer",
                "phase": "CONSTRUCTION",
                "stage": "fallback",
                "content": f"Generated {len(code_files)} file(s) via fallback (LLM output unparseable)",
                "thinking": "",
            })

        print(f"[DEVELOPER] Returning {len(code_files)} code files")
        return {
            "code_files": code_files,
            "reasoning_logs": logs,
        }

    except Exception as e:
        error_msg = f"Developer error: {str(e)}"
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return {
            "code_files": [],
            "reasoning_logs": [
                {"agent": "Developer", "phase": "CONSTRUCTION", "stage": "error", "content": error_msg, "thinking": ""}
            ],
        }


def _generate_fallback(user_request: str, specs: str) -> list[dict]:
    """Generate sensible fallback code based on the actual request."""
    req = user_request.lower()

    # Fibonacci
    if "fibonacci" in req or "fib" in req:
        content = (
            "def fibonacci(n: int) -> int:\n"
            "    \"\"\"Return the nth Fibonacci number (0-indexed).\"\"\"\n"
            "    if n < 0:\n"
            "        raise ValueError(f'n must be non-negative, got {n}')\n"
            "    if n < 2:\n"
            "        return n\n"
            "    a, b = 0, 1\n"
            "    for _ in range(2, n + 1):\n"
            "        a, b = b, a + b\n"
            "    return b\n"
            "\n\n"
            "def fibonacci_sequence(length: int) -> list[int]:\n"
            "    \"\"\"Return Fibonacci sequence of given length.\"\"\"\n"
            "    return [fibonacci(i) for i in range(length)]\n"
        )
        files = [{"path": "fibonacci.py", "content": content, "language": "python"}]
        if "test" in req:
            test_content = (
                "import pytest\n"
                "from fibonacci import fibonacci, fibonacci_sequence\n"
                "\n\n"
                "def test_base_cases() -> None:\n"
                "    assert fibonacci(0) == 0\n"
                "    assert fibonacci(1) == 1\n"
                "\n\n"
                "def test_known_values() -> None:\n"
                "    assert fibonacci(5) == 5\n"
                "    assert fibonacci(10) == 55\n"
                "    assert fibonacci(20) == 6765\n"
                "\n\n"
                "def test_sequence() -> None:\n"
                "    assert fibonacci_sequence(7) == [0, 1, 1, 2, 3, 5, 8]\n"
                "\n\n"
                "def test_negative_raises() -> None:\n"
                "    with pytest.raises(ValueError):\n"
                "        fibonacci(-1)\n"
            )
            files.append({"path": "test_fibonacci.py", "content": test_content, "language": "python"})
        return files

    # Sorting
    if "sort" in req:
        content = (
            "def bubble_sort(arr: list) -> list:\n"
            "    arr = list(arr)\n"
            "    n = len(arr)\n"
            "    for i in range(n):\n"
            "        for j in range(0, n - i - 1):\n"
            "            if arr[j] > arr[j + 1]:\n"
            "                arr[j], arr[j + 1] = arr[j + 1], arr[j]\n"
            "    return arr\n"
        )
        return [{"path": "sort.py", "content": content, "language": "python"}]

    # Generic function request
    if any(w in req for w in ("function", "def ", "implement", "write a", "create a")):
        content = (
            "# Generated stub — LLM output was not parseable\n"
            "# Please refine your request and try again.\n\n"
            "def solution():\n"
            "    raise NotImplementedError('Implement the requested logic here')\n"
        )
        return [{"path": "solution.py", "content": content, "language": "python"}]

    # Minimal app fallback
    content = (
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "@app.get('/')\n"
        "def root():\n"
        "    return {'message': 'Application running'}\n"
    )
    return [{"path": "main.py", "content": content, "language": "python"}]


# ── Delivery Engineer Node ─────────────────────────────────────────────────────

def delivery_engineer_node(state: AgentState) -> dict[str, Any]:
    """Delivery engineer: generate CI/CD and deployment configs."""
    specs = state.get("specs", "")
    code_files = state.get("code_files", [])
    token: CancelToken | None = state.get("_cancel_token")  # type: ignore[assignment]

    print(f"\n=== DELIVERY ===")

    logs: list[dict] = []

    try:
        cicd_yaml = ""
        dockerfile = ""

        if HAS_LLM:
            logs.append({
                "agent": "Delivery",
                "phase": "CONSTRUCTION",
                "stage": "deployment",
                "content": "Generating CI/CD pipeline and Dockerfile...",
                "thinking": "",
            })
            delivery_prompt = pm.get_prompt("delivery_engineer", "system")
            if not delivery_prompt:
                delivery_prompt = (
                    "You are the Delivery Engineer. Generate CI/CD and Docker configs.\n\n"
                    "===CICD_START===\n```yaml\n<GitHub Actions YAML>\n```\n===CICD_END===\n\n"
                    "===DOCKERFILE_START===\n```dockerfile\n<Dockerfile>\n```\n===DOCKERFILE_END==="
                )

            file_list = "\n".join(
                f["path"] if isinstance(f, dict) else f.path
                for f in code_files
            )
            context = (
                f"Specs summary:\n{specs[:1500]}\n\n"
                f"Generated files:\n{file_list or '(none yet)'}"
            )

            thinking, response_text = _call_llm(delivery_prompt, context, token=token)

            # Parse CICD block
            cicd_match = re.search(
                r"===CICD_START===.*?```ya?ml\s*(.*?)```.*?===CICD_END===",
                response_text, re.DOTALL
            )
            if cicd_match:
                cicd_yaml = cicd_match.group(1).strip()

            # Parse Dockerfile block
            df_match = re.search(
                r"===DOCKERFILE_START===.*?```(?:dockerfile)?\s*(.*?)```.*?===DOCKERFILE_END===",
                response_text, re.DOTALL | re.IGNORECASE
            )
            if df_match:
                dockerfile = df_match.group(1).strip()

            logs.append({
                "agent": "Delivery",
                "phase": "CONSTRUCTION",
                "stage": "deployment",
                "content": "Generated CI/CD and Dockerfile via LLM",
                "thinking": thinking,
            })

        # Fallback templates
        if not cicd_yaml:
            cicd_yaml = (
                "name: CI\n\n"
                "on:\n"
                "  push:\n"
                "    branches: [main]\n"
                "  pull_request:\n"
                "    branches: [main]\n\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - uses: actions/setup-python@v5\n"
                "        with:\n"
                "          python-version: '3.11'\n"
                "      - run: pip install -r requirements.txt\n"
                "        if: hashFiles('requirements.txt') != ''\n"
                "      - run: python -m pytest\n"
                "        if: hashFiles('test_*.py') != '' || hashFiles('tests/*.py') != ''\n"
            )

        if not dockerfile:
            dockerfile = (
                "FROM python:3.11-slim\n\n"
                "WORKDIR /app\n\n"
                "COPY requirements.txt* ./\n"
                "RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true\n\n"
                "COPY . .\n\n"
                "USER nobody\n\n"
                "CMD [\"python\", \"main.py\"]\n"
            )

        if not logs:
            logs.append({
                "agent": "Delivery",
                "phase": "CONSTRUCTION",
                "stage": "deployment",
                "content": "Generated CI/CD and Dockerfile (fallback templates)",
                "thinking": "",
            })

        return {
            "cicd_yaml": cicd_yaml,
            "dockerfile": dockerfile,
            "reasoning_logs": logs,
        }

    except Exception as e:
        error_msg = f"Delivery error: {str(e)}"
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return {
            "cicd_yaml": "",
            "dockerfile": "",
            "reasoning_logs": [
                {"agent": "Delivery", "phase": "CONSTRUCTION", "stage": "error", "content": error_msg, "thinking": ""}
            ],
        }
