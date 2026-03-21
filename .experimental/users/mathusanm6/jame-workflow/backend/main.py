#!/usr/bin/env python3
"""Multi-Agent Software Factory — CLI entry point.

AI-DLC phases:
  INCEPTION:     Architect agent (requirements + application design)
  CONSTRUCTION:  Developer agent (code generation) +
                 Quality Engineer agent (build and test, with fix loop)

Usage:
    python main.py                          # interactive prompt
    python main.py "A FastAPI Task Manager" # direct input
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from graph import build_graph
from state import AgentState


OUTPUT_DIR  = Path(os.getenv("OUTPUT_DIR", "output"))
PROJECT_DIR = OUTPUT_DIR / "project"


def _sanitize_file_path(raw_path: str) -> str:
    """Clean a file path of markdown artefacts before writing to disk."""
    path = re.sub(r'[#*`]', '', raw_path)
    path = re.sub(r'^\s*[\d]+\.\s*', '', path)
    path = re.sub(r'^\s*[-•]\s*', '', path)
    path = path.strip().strip('"').strip("'")
    path = re.sub(r'^\./', '', path)
    path = re.sub(r'[<>"|?*]', '', path)
    return path


def save_artifacts(final_state: dict) -> None:
    """Write all generated artifacts to the output/ directory.

    Saves:
      - C4 diagrams       → output/c4_diagrams.md
      - Specifications    → output/specifications.md
      - Code files        → output/project/<path>
      - CI/CD workflow    → output/project/.github/workflows/ci.yml
      - Dockerfile        → output/project/Dockerfile
      - Reasoning trace   → output/reasoning_trace.json  (AI-DLC audit trail)
      - QA issues         → output/qa_issues.json
      - README_WARNING.md → output/project/ if QA did not pass
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    PROJECT_DIR.mkdir(exist_ok=True)

    diagrams = final_state.get("diagrams", "")
    if diagrams:
        (OUTPUT_DIR / "c4_diagrams.md").write_text(diagrams, encoding="utf-8")
        print("  📐 Diagrams  → output/c4_diagrams.md")

    specs = final_state.get("specs", "")
    if specs:
        (OUTPUT_DIR / "specifications.md").write_text(specs, encoding="utf-8")
        print("  📋 Specs     → output/specifications.md")

    code_files  = final_state.get("code_files", [])
    saved_count = 0
    for f in code_files:
        clean_path = _sanitize_file_path(f["path"] if isinstance(f, dict) else f.path)
        if not clean_path:
            print(f"  ⚠️  Skipping file with invalid path: {f!r}")
            continue
        file_path = PROJECT_DIR / clean_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        content = f["content"] if isinstance(f, dict) else f.content
        file_path.write_text(content, encoding="utf-8")
        print(f"  📄 Code      → output/project/{clean_path}")
        saved_count += 1

    if code_files:
        print(f"  📦 Total     → {saved_count} file(s) saved to output/project/")

    cicd = final_state.get("cicd_yaml", "")
    if cicd:
        cicd_dir = PROJECT_DIR / ".github" / "workflows"
        cicd_dir.mkdir(parents=True, exist_ok=True)
        (cicd_dir / "ci.yml").write_text(cicd, encoding="utf-8")
        print("  🔧 CI/CD     → output/project/.github/workflows/ci.yml")

    dockerfile = final_state.get("dockerfile", "")
    if dockerfile:
        (PROJECT_DIR / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        print("  🐳 Docker    → output/project/Dockerfile")

    logs = final_state.get("reasoning_logs", [])
    if logs:
        (OUTPUT_DIR / "reasoning_trace.json").write_text(
            json.dumps(logs, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("  🧠 Trace     → output/reasoning_trace.json")

    qa_issues = final_state.get("qa_issues", [])
    if qa_issues:
        serializable = [
            i if isinstance(i, dict) else i.model_dump()
            for i in qa_issues
        ]
        (OUTPUT_DIR / "qa_issues.json").write_text(
            json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("  🎫 QA Issues → output/qa_issues.json")

    qa_passed      = final_state.get("qa_passed", False)
    iteration      = final_state.get("iteration", 0)
    max_iterations = final_state.get("max_iterations", 3)

    if not qa_passed and iteration >= max_iterations:
        warning_lines = [
            "# WARNING: Code Quality Not Verified",
            "",
            f"The Quality Engineer did **not pass** after {iteration} iteration(s) "
            f"(max: {max_iterations}).",
            "",
            "The generated code may be **incomplete or non-compliant** with the "
            "original specifications. Review the issues below before using this code.",
            "",
            "## Outstanding Issues",
            "",
        ]
        if qa_issues:
            for iss in qa_issues:
                iss_dict = iss if isinstance(iss, dict) else iss.model_dump()
                sev  = iss_dict.get("severity", "unknown").upper()
                file = iss_dict.get("file", "GENERAL")
                desc = iss_dict.get("description", "No description")
                warning_lines.append(f"- **[{sev}]** `{file}`: {desc}")
        else:
            warning_lines.append(final_state.get("qa_feedback", "No details available."))

        warning_lines += [
            "",
            "## Recommendation",
            "",
            "1. Review each issue above.",
            "2. Fix the code manually or re-run the pipeline with adjusted specs.",
            "3. Run tests: `pytest`",
        ]
        (PROJECT_DIR / "README_WARNING.md").write_text(
            "\n".join(warning_lines), encoding="utf-8"
        )
        print("  ⚠️  Warning  → output/project/README_WARNING.md")


def print_reasoning_summary(final_state: dict) -> None:
    """Print the AI-DLC reasoning trace (Plan / Act / Reason per agent)."""
    logs = final_state.get("reasoning_logs", [])
    if not logs:
        return

    print("\n" + "=" * 60)
    print("🧠 AI-DLC REASONING TRACE")
    print("=" * 60)
    for entry in logs:
        agent   = entry.get("agent", "?").upper()
        phase   = entry.get("phase", "?")
        stage   = entry.get("stage", "")
        content = entry.get("content", "")
        label   = f"{phase}/{stage}" if stage else phase
        print(f"  [{agent:>18}] {label}: {content}")


def main() -> None:
    load_dotenv()

    if not os.getenv("SNOWFLAKE_API_KEY"):
        print("Error: SNOWFLAKE_API_KEY not found.")
        print("  Copy .env.example to .env and fill in your Snowflake Cortex credentials.")
        sys.exit(1)

    if len(sys.argv) > 1:
        user_request = " ".join(sys.argv[1:])
    else:
        print("Multi-Agent Software Factory (AI-DLC)")
        print("─" * 40)
        user_request = input("Describe the application to build:\n> ").strip()
        if not user_request:
            print("No input provided. Exiting.")
            sys.exit(0)

    print(f"\nStarting AI-DLC pipeline for: \"{user_request}\"\n")

    graph = build_graph()

    initial_state: AgentState = {
        "user_request":   user_request,
        "specs":          "",
        "diagrams":       "",
        "code_files":     [],
        "cicd_yaml":      "",
        "dockerfile":     "",
        "qa_passed":      False,
        "qa_feedback":    "",
        "qa_issues":      [],
        "iteration":      0,
        "max_iterations": 3,
        "reasoning_logs": [],
        "scope":          "system",
    }

    final_state = graph.invoke(initial_state)

    print("\n" + "=" * 60)
    print("SAVING ARTIFACTS")
    print("=" * 60)
    save_artifacts(final_state)

    print_reasoning_summary(final_state)

    if final_state.get("qa_passed"):
        print("\n" + "=" * 60)
        print("AI-DLC Pipeline complete. QA decision: PASS")
        print("=" * 60 + "\n")
    else:
        print("\n" + "=" * 60)
        print("AI-DLC Pipeline complete. QA decision: FAIL — see output/project/README_WARNING.md")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
