#!/usr/bin/env python3
"""Multi-Agent Software Factory — CLI entry point.

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


OUTPUT_DIR = Path("output")
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
    """Write all generated artifacts to the output/ directory."""

    OUTPUT_DIR.mkdir(exist_ok=True)
    PROJECT_DIR.mkdir(exist_ok=True)

    # ── Save diagrams ──────────────────────────────────────────
    diagrams = final_state.get("diagrams", "")
    if diagrams:
        (OUTPUT_DIR / "c4_diagrams.md").write_text(diagrams, encoding="utf-8")
        print("  📐 Diagrams  → output/c4_diagrams.md")

    # ── Save specs ─────────────────────────────────────────────
    specs = final_state.get("specs", "")
    if specs:
        (OUTPUT_DIR / "specifications.md").write_text(specs, encoding="utf-8")
        print("  📋 Specs     → output/specifications.md")

    # ── Save code files ────────────────────────────────────────
    code_files = final_state.get("code_files", [])
    saved_count = 0
    for f in code_files:
        clean_path = _sanitize_file_path(f["path"])
        if not clean_path:
            print(f"  ⚠️  Skipping file with invalid path: {f['path']!r}")
            continue
        file_path = PROJECT_DIR / clean_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(f["content"], encoding="utf-8")
        print(f"  📄 Code      → output/project/{clean_path}")
        saved_count += 1

    if code_files:
        print(f"  📦 Total     → {saved_count} files saved to output/project/")

    # ── Save CI/CD ─────────────────────────────────────────────
    cicd = final_state.get("cicd_yaml", "")
    if cicd:
        cicd_dir = PROJECT_DIR / ".github" / "workflows"
        cicd_dir.mkdir(parents=True, exist_ok=True)
        (cicd_dir / "ci.yml").write_text(cicd, encoding="utf-8")
        print("  🔧 CI/CD     → output/project/.github/workflows/ci.yml")

    # ── Save Dockerfile ────────────────────────────────────────
    dockerfile = final_state.get("dockerfile", "")
    if dockerfile:
        (PROJECT_DIR / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        print("  🐳 Docker    → output/project/Dockerfile")

    # ── Save reasoning trace ───────────────────────────────────
    logs = final_state.get("reasoning_logs", [])
    if logs:
        (OUTPUT_DIR / "reasoning_trace.json").write_text(
            json.dumps(logs, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print("  🧠 Trace     → output/reasoning_trace.json")

    # ── Save QA issues ─────────────────────────────────────────
    qa_issues = final_state.get("qa_issues", [])
    if qa_issues:
        (OUTPUT_DIR / "qa_issues.json").write_text(
            json.dumps(qa_issues, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print("  🎫 QA Issues → output/qa_issues.json")

    # ── README_WARNING.md if QA never passed ───────────────────
    qa_passed = final_state.get("qa_passed", False)
    iteration = final_state.get("iteration", 0)
    max_iterations = final_state.get("max_iterations", 2)

    if not qa_passed and iteration >= max_iterations:
        warning_lines = [
            "# ⚠️ WARNING: Code Quality Not Verified",
            "",
            f"The QA agent did **not pass** after {iteration} iteration(s) "
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
                sev = iss.get("severity", "unknown").upper()
                file = iss.get("file", "GENERAL")
                desc = iss.get("description", "No description")
                warning_lines.append(f"- **[{sev}]** `{file}`: {desc}")
        else:
            feedback = final_state.get("qa_feedback", "No details available.")
            warning_lines.append(feedback)

        warning_lines.extend([
            "",
            "## Recommendation",
            "",
            "1. Review each issue above.",
            "2. Fix the code manually or re-run the pipeline with adjusted specs.",
            "3. Run tests: `cd backend && pytest`",
        ])

        warning_path = PROJECT_DIR / "README_WARNING.md"
        warning_path.write_text("\n".join(warning_lines), encoding="utf-8")
        print("  ⚠️  Warning  → output/project/README_WARNING.md")


def print_reasoning_summary(final_state: dict) -> None:
    """Print a compact reasoning summary at the end."""
    logs = final_state.get("reasoning_logs", [])
    if not logs:
        return

    print("\n" + "=" * 60)
    print("🧠 REASONING TRACE SUMMARY")
    print("=" * 60)

    for entry in logs:
        phase_icon = {"plan": "📋", "act": "⚡", "reason": "💭"}.get(entry["phase"], "•")
        print(f"  {phase_icon} [{entry['agent'].upper():>10}] {entry['phase'].upper():>6}: {entry['content']}")


def main() -> None:
    load_dotenv()

    if not os.getenv("SNOWFLAKE_API_KEY"):
        print("❌ Error: SNOWFLAKE_API_KEY not found.")
        print("   Copy .env.example to .env and add your Snowflake Cortex credentials.")
        sys.exit(1)

    # ── Get user request ───────────────────────────────────────
    if len(sys.argv) > 1:
        user_request = " ".join(sys.argv[1:])
    else:
        print("🏭 Multi-Agent Software Factory")
        print("─" * 40)
        user_request = input("Describe the application to build:\n> ").strip()
        if not user_request:
            print("No input provided. Exiting.")
            sys.exit(0)

    print(f"\n🚀 Starting pipeline for: \"{user_request}\"\n")

    # ── Build and run the graph ────────────────────────────────
    graph = build_graph()

    initial_state: AgentState = {
        "user_request": user_request,
        "specs": "",
        "diagrams": "",
        "code_files": [],
        "cicd_yaml": "",
        "dockerfile": "",
        "qa_passed": False,
        "qa_feedback": "",
        "qa_issues": [],
        "iteration": 0,
        "max_iterations": 2,
        "reasoning_logs": [],
    }

    final_state = graph.invoke(initial_state)

    # ── Save everything ────────────────────────────────────────
    print("\n" + "=" * 60)
    print("💾 SAVING ARTIFACTS")
    print("=" * 60)
    save_artifacts(final_state)

    # ── Summary ────────────────────────────────────────────────
    print_reasoning_summary(final_state)

    qa_passed = final_state.get("qa_passed", False)
    if qa_passed:
        print("\n" + "=" * 60)
        print("✅ Pipeline complete! All QA checks passed.")
        print("=" * 60 + "\n")
    else:
        print("\n" + "=" * 60)
        print("⚠️  Pipeline complete with QA warnings. See output/project/README_WARNING.md")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
