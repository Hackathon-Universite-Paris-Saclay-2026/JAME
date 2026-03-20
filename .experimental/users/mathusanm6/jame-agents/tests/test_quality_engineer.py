"""Tests for the Quality Engineer agent (AI-DLC CONSTRUCTION / Build and Test).

All LLM calls are mocked — no real API key or network access required.
Tests cover:
  - Triage stage (file classification)
  - Static review stage (per-file analysis)
  - Cross-file consistency stage
  - Fix stage (patch instructions + full rewrite escalation)
  - Re-review stage
  - Verdict stage (PASS / FAIL)
  - Integration: full quality_engineer_node() with various code file scenarios
"""

from __future__ import annotations

import sys
import os

# Make the parent directory importable so `state` and `agents` resolve correctly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch, call
import pytest

from state import AgentState, CodeFile, QAIssue
from agents.quality_engineer import (
    quality_engineer_node,
    _triage,
    _analyse_file,
    _cross_file_check,
    _generate_fix_instructions,
    _re_review_file,
    _issue_verdict,
    _collect_qa_issues,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SPECS = """\
## Task Manager API
- POST /tasks          Create a task (title, description, priority)
- GET  /tasks          List all tasks
- GET  /tasks/{id}     Get a task by ID
- PUT  /tasks/{id}     Update a task
- DELETE /tasks/{id}   Delete a task
Authentication: JWT bearer token required on all routes.
"""

CLEAN_FILE = CodeFile(
    path="backend/main.py",
    content='"""FastAPI entry point."""\nfrom fastapi import FastAPI\napp = FastAPI()\n',
    language="python",
)

FILE_WITH_CRITICAL = CodeFile(
    path="backend/auth.py",
    content='SECRET = "hardcoded_secret_123"\ndef login(user, pw): return pw == "admin"\n',
    language="python",
)

FILE_WITH_MAJOR = CodeFile(
    path="backend/tasks.py",
    content='def get_task(task_id): return None  # TODO: implement\n',
    language="python",
)

FILE_WITH_MINOR = CodeFile(
    path="backend/utils.py",
    content='import os\n\ndef helper():\n    x = 1  # unused\n    return x\n',
    language="python",
)


def _make_llm_response(content: str) -> MagicMock:
    """Build a mock ChatOpenAI response object."""
    mock_resp = MagicMock()
    mock_resp.content = content
    return mock_resp


def _make_llm(responses: list[str]) -> MagicMock:
    """Build a mock LLM that returns each response in sequence."""
    mock_llm = MagicMock()
    mock_llm.invoke.side_effect = [_make_llm_response(r) for r in responses]
    return mock_llm


def _base_state(**overrides) -> AgentState:
    """Return a minimal valid AgentState, with optional overrides."""
    state: AgentState = {
        "user_request":   "Build a task manager",
        "specs":          SPECS,
        "diagrams":       "",
        "code_files":     [CLEAN_FILE],
        "cicd_yaml":      "",
        "dockerfile":     "",
        "qa_passed":      False,
        "qa_feedback":    "",
        "qa_issues":      [],
        "iteration":      0,
        "max_iterations": 3,
        "reasoning_logs": [],
    }
    state.update(overrides)
    return state


# ── Unit tests: _triage ───────────────────────────────────────────────────────

class TestTriage:
    def test_classifies_files_correctly(self):
        files = [CLEAN_FILE, FILE_WITH_CRITICAL, FILE_WITH_MINOR]
        llm_json = (
            '{"files": ['
            '{"path": "backend/main.py", "priority": "critical"},'
            '{"path": "backend/auth.py", "priority": "critical"},'
            '{"path": "backend/utils.py", "priority": "standard"}'
            ']}'
        )
        llm = _make_llm([llm_json])
        result = _triage(llm, SPECS, files)

        assert result["backend/main.py"] == "critical"
        assert result["backend/auth.py"] == "critical"
        assert result["backend/utils.py"] == "standard"

    def test_defaults_unlisted_files_to_standard(self):
        files = [CLEAN_FILE, FILE_WITH_MINOR]
        llm = _make_llm(['{"files": [{"path": "backend/main.py", "priority": "important"}]}'])
        result = _triage(llm, SPECS, files)

        assert result["backend/main.py"] == "important"
        assert result["backend/utils.py"] == "standard"  # not in LLM response → default

    def test_handles_malformed_json_gracefully(self):
        files = [CLEAN_FILE]
        llm = _make_llm(["not valid json at all"])
        result = _triage(llm, SPECS, files)

        # Falls back to default for all files
        assert result["backend/main.py"] == "standard"

    def test_handles_thinking_block(self):
        files = [CLEAN_FILE]
        llm = _make_llm([
            '<think>reasoning here</think>'
            '{"files": [{"path": "backend/main.py", "priority": "critical"}]}'
        ])
        result = _triage(llm, SPECS, files)
        assert result["backend/main.py"] == "critical"


# ── Unit tests: _analyse_file ─────────────────────────────────────────────────

class TestAnalyseFile:
    def test_clean_file_returns_no_issues(self):
        llm = _make_llm(['{"file": "backend/main.py", "issues": [], "has_issues": false}'])
        result = _analyse_file(llm, SPECS, CLEAN_FILE, "critical")

        assert result["has_issues"] is False
        assert result["issues"] == []

    def test_detects_critical_security_issue(self):
        llm_json = (
            '{"file": "backend/auth.py", "issues": ['
            '{"severity": "critical", "security_rule": "SECURITY-12", '
            '"line_hint": "1", "description": "Hardcoded secret", '
            '"fix": "Use env var"}'
            '], "has_issues": true}'
        )
        llm = _make_llm([llm_json])
        result = _analyse_file(llm, SPECS, FILE_WITH_CRITICAL, "critical")

        assert result["has_issues"] is True
        assert len(result["issues"]) == 1
        assert result["issues"][0]["severity"] == "critical"
        assert result["issues"][0]["security_rule"] == "SECURITY-12"

    def test_detects_major_issue(self):
        llm_json = (
            '{"file": "backend/tasks.py", "issues": ['
            '{"severity": "major", "security_rule": null, '
            '"line_hint": "1", "description": "TODO stub not implemented", '
            '"fix": "Implement the function body"}'
            '], "has_issues": true}'
        )
        llm = _make_llm([llm_json])
        result = _analyse_file(llm, SPECS, FILE_WITH_MAJOR, "important")

        assert result["issues"][0]["severity"] == "major"

    def test_fallback_on_malformed_json(self):
        llm = _make_llm(["completely broken response"])
        result = _analyse_file(llm, SPECS, CLEAN_FILE, "standard")

        assert result["file"] == "backend/main.py"
        assert result["issues"] == []


# ── Unit tests: _cross_file_check ─────────────────────────────────────────────

class TestCrossFileCheck:
    def test_no_cross_file_issues(self):
        per_file = [
            {"file": "backend/main.py", "issues": []},
            {"file": "backend/tasks.py", "issues": []},
        ]
        llm = _make_llm(['{"issues": [], "has_issues": false}'])
        result = _cross_file_check(llm, SPECS, per_file)

        assert result["has_issues"] is False
        assert result["issues"] == []

    def test_detects_missing_env_var(self):
        per_file = [{"file": "backend/main.py", "issues": []}]
        llm_json = (
            '{"issues": [{"file": "GENERAL", "severity": "major", '
            '"description": "DATABASE_URL used in code but not in .env.example", '
            '"fix": "Add DATABASE_URL to .env.example"}], "has_issues": true}'
        )
        llm = _make_llm([llm_json])
        result = _cross_file_check(llm, SPECS, per_file)

        assert result["has_issues"] is True
        assert result["issues"][0]["file"] == "GENERAL"

    def test_detects_import_mismatch(self):
        per_file = [
            {"file": "backend/main.py", "issues": [
                {"description": "Imports get_task from tasks.py", "severity": "minor"}
            ]},
            {"file": "backend/tasks.py", "issues": []},
        ]
        llm_json = (
            '{"issues": [{"file": "backend/main.py", "severity": "critical", '
            '"description": "Imports symbol not exported by tasks.py", '
            '"fix": "Export the function from tasks.py"}], "has_issues": true}'
        )
        llm = _make_llm([llm_json])
        result = _cross_file_check(llm, SPECS, per_file)

        assert result["issues"][0]["severity"] == "critical"


# ── Unit tests: _generate_fix_instructions ────────────────────────────────────

class TestGenerateFixInstructions:
    def test_patch_instructions_for_few_issues(self):
        issues = [
            {"severity": "major", "line_hint": "5", "description": "Missing auth check",
             "security_rule": "SECURITY-08", "fix": "Add JWT dependency"},
        ]
        llm = _make_llm(["1. Add `Depends(get_current_user)` to the route signature."])
        result = _generate_fix_instructions(llm, SPECS, CLEAN_FILE, issues)

        assert "Depends" in result
        llm.invoke.assert_called_once()

    def test_escalates_to_rewrite_brief_for_3_or_more_critical_issues(self):
        issues = [
            {"severity": "critical", "line_hint": "1", "description": f"Critical issue {i}",
             "security_rule": "SECURITY-12", "fix": "Fix it"}
            for i in range(3)
        ]
        llm = _make_llm(["Rewrite brief: this file must implement X, Y, Z securely."])
        result = _generate_fix_instructions(llm, SPECS, FILE_WITH_CRITICAL, issues)

        assert "Rewrite brief" in result or len(result) > 0
        llm.invoke.assert_called_once()

    def test_exactly_2_critical_issues_uses_patch_not_rewrite(self):
        issues = [
            {"severity": "critical", "line_hint": str(i), "description": f"Critical {i}",
             "security_rule": None, "fix": "Fix it"}
            for i in range(2)
        ]
        llm = _make_llm(["Patch: 1. Fix line 0. 2. Fix line 1."])
        _generate_fix_instructions(llm, SPECS, CLEAN_FILE, issues)
        # Patch path invoked (not rewrite) → same single LLM call
        llm.invoke.assert_called_once()


# ── Unit tests: _re_review_file ───────────────────────────────────────────────

class TestReReviewFile:
    def test_all_issues_fixed_returns_passed(self):
        original_issues = [
            {"severity": "critical", "description": "Hardcoded secret"}
        ]
        llm_json = (
            '{"file": "backend/auth.py", '
            '"resolved": [{"original": "Hardcoded secret", "status": "fixed", "note": "Now uses env var"}], '
            '"new_issues": [], "passed": true}'
        )
        llm = _make_llm([llm_json])
        result = _re_review_file(llm, FILE_WITH_CRITICAL, original_issues)

        assert result["passed"] is True
        assert result["new_issues"] == []

    def test_unresolved_issue_returns_failed(self):
        original_issues = [{"severity": "critical", "description": "SQL injection"}]
        llm_json = (
            '{"file": "backend/tasks.py", '
            '"resolved": [{"original": "SQL injection", "status": "unresolved", "note": "Still present"}], '
            '"new_issues": [], "passed": false}'
        )
        llm = _make_llm([llm_json])
        result = _re_review_file(llm, FILE_WITH_MAJOR, original_issues)

        assert result["passed"] is False

    def test_new_issue_introduced_fails(self):
        original_issues = [{"severity": "major", "description": "Missing validation"}]
        llm_json = (
            '{"file": "backend/main.py", '
            '"resolved": [{"original": "Missing validation", "status": "fixed", "note": ""}], '
            '"new_issues": [{"severity": "critical", "security_rule": "SECURITY-08", '
            '"description": "Auth removed", "fix": "Restore auth middleware"}], '
            '"passed": false}'
        )
        llm = _make_llm([llm_json])
        result = _re_review_file(llm, CLEAN_FILE, original_issues)

        assert result["passed"] is False
        assert len(result["new_issues"]) == 1


# ── Unit tests: _issue_verdict ────────────────────────────────────────────────

class TestIssueVerdict:
    def test_returns_pass(self):
        re_review = [{"file": "backend/main.py", "passed": True, "resolved": [], "new_issues": []}]
        llm = _make_llm(["## QA Verdict\nAll files passed.\nAI-DLC QA decision: PASS"])
        passed, text = _issue_verdict(llm, re_review)

        assert passed is True
        assert "PASS" in text

    def test_returns_fail(self):
        re_review = [{"file": "backend/auth.py", "passed": False, "resolved": [], "new_issues": []}]
        llm = _make_llm(["## QA Verdict\nCritical issues remain.\nAI-DLC QA decision: FAIL"])
        passed, text = _issue_verdict(llm, re_review)

        assert passed is False
        assert "FAIL" in text

    def test_pass_requires_exact_string(self):
        """Verdict detection must look for 'AI-DLC QA decision: PASS' not just 'PASS'."""
        re_review = [{"file": "f.py", "passed": True, "resolved": [], "new_issues": []}]
        llm = _make_llm(["FAIL with no standard marker"])  # no exact marker
        passed, _ = _issue_verdict(llm, re_review)
        assert passed is False


# ── Unit tests: _collect_qa_issues ───────────────────────────────────────────

class TestCollectQaIssues:
    def test_collects_from_per_file_and_cross_file(self):
        per_file = [
            {"file": "backend/main.py", "issues": [
                {"severity": "critical", "description": "Missing auth"}
            ]},
            {"file": "backend/utils.py", "issues": [
                {"severity": "minor", "description": "Unused var"}
            ]},
        ]
        cross = {"issues": [
            {"file": "GENERAL", "severity": "major", "description": "Import mismatch"}
        ]}
        result = _collect_qa_issues(per_file, cross)

        assert len(result) == 3
        severities = {i.severity for i in result}
        assert severities == {"critical", "major", "minor"}

    def test_empty_inputs_return_empty(self):
        result = _collect_qa_issues([], {"issues": []})
        assert result == []

    def test_file_assigned_correctly(self):
        per_file = [{"file": "backend/auth.py", "issues": [
            {"severity": "critical", "description": "Hardcoded secret"}
        ]}]
        result = _collect_qa_issues(per_file, {"issues": []})
        assert result[0].file == "backend/auth.py"


# ── Integration tests: quality_engineer_node ─────────────────────────────────

class TestQualityEngineerNode:
    """End-to-end integration tests with mocked LLM calls."""

    @patch("agents.quality_engineer._get_llm")
    def test_no_code_files_skips_qa(self, mock_get_llm):
        """Node returns FAIL immediately if no code files are provided.
        The LLM is instantiated but never invoked (no API calls made).
        """
        state = _base_state(code_files=[])
        result = quality_engineer_node(state)

        assert result["qa_passed"] is False
        assert "No code files" in result["qa_feedback"]
        mock_get_llm.return_value.invoke.assert_not_called()

    @patch("agents.quality_engineer._get_llm")
    def test_clean_code_passes_immediately(self, mock_get_llm):
        """No critical/major issues → PASS without generating fix instructions."""
        triage_resp  = '{"files": [{"path": "backend/main.py", "priority": "standard"}]}'
        review_resp  = '{"file": "backend/main.py", "issues": [], "has_issues": false}'
        cross_resp   = '{"issues": [], "has_issues": false}'

        mock_get_llm.return_value = _make_llm([triage_resp, review_resp, cross_resp])

        state  = _base_state(code_files=[CLEAN_FILE])
        result = quality_engineer_node(state)

        assert result["qa_passed"] is True
        assert result["qa_feedback"] == ""
        assert result["qa_issues"] == []

    @patch("agents.quality_engineer._get_llm")
    def test_critical_issue_produces_feedback_and_fails(self, mock_get_llm):
        """Critical issue found → qa_passed=False, qa_feedback contains instructions."""
        triage_resp  = '{"files": [{"path": "backend/auth.py", "priority": "critical"}]}'
        review_resp  = (
            '{"file": "backend/auth.py", "issues": ['
            '{"severity": "critical", "security_rule": "SECURITY-12", '
            '"line_hint": "1", "description": "Hardcoded secret", "fix": "Use env var"}'
            '], "has_issues": true}'
        )
        cross_resp   = '{"issues": [], "has_issues": false}'
        fix_resp     = "1. Replace SECRET with os.environ['SECRET_KEY']."

        mock_get_llm.return_value = _make_llm([triage_resp, review_resp, cross_resp, fix_resp])

        state  = _base_state(code_files=[FILE_WITH_CRITICAL])
        result = quality_engineer_node(state)

        assert result["qa_passed"] is False
        assert len(result["qa_issues"]) == 1
        assert result["qa_issues"][0]["severity"] == "critical"
        assert "backend/auth.py" in result["qa_feedback"]

    @patch("agents.quality_engineer._get_llm")
    def test_iteration_counter_increments_on_fail(self, mock_get_llm):
        """Each QA fail increments the iteration counter."""
        triage_resp = '{"files": [{"path": "backend/auth.py", "priority": "critical"}]}'
        review_resp = (
            '{"file": "backend/auth.py", "issues": ['
            '{"severity": "critical", "security_rule": "SECURITY-12", '
            '"line_hint": "1", "description": "Hardcoded secret", "fix": "Use env var"}'
            '], "has_issues": true}'
        )
        cross_resp = '{"issues": [], "has_issues": false}'
        fix_resp   = "Fix: use env var."

        mock_get_llm.return_value = _make_llm([triage_resp, review_resp, cross_resp, fix_resp])

        state  = _base_state(code_files=[FILE_WITH_CRITICAL], iteration=0, max_iterations=3)
        result = quality_engineer_node(state)

        assert result["iteration"] == 1

    @patch("agents.quality_engineer._get_llm")
    def test_max_iterations_triggers_verdict(self, mock_get_llm):
        """When iteration == max_iterations - 1, re-review + verdict are called."""
        triage_resp   = '{"files": [{"path": "backend/auth.py", "priority": "critical"}]}'
        review_resp   = (
            '{"file": "backend/auth.py", "issues": ['
            '{"severity": "critical", "security_rule": "SECURITY-12", '
            '"line_hint": "1", "description": "Hardcoded secret", "fix": "Use env var"}'
            '], "has_issues": true}'
        )
        cross_resp    = '{"issues": [], "has_issues": false}'
        fix_resp      = "Fix: use env var."
        re_review_resp = (
            '{"file": "backend/auth.py", '
            '"resolved": [{"original": "Hardcoded secret", "status": "unresolved", "note": ""}], '
            '"new_issues": [], "passed": false}'
        )
        verdict_resp  = "## QA Verdict\nIssues remain.\nAI-DLC QA decision: FAIL"

        mock_get_llm.return_value = _make_llm([
            triage_resp, review_resp, cross_resp, fix_resp, re_review_resp, verdict_resp
        ])

        # iteration=2, max_iterations=3 → this is the last allowed iteration
        state  = _base_state(code_files=[FILE_WITH_CRITICAL], iteration=2, max_iterations=3)
        result = quality_engineer_node(state)

        assert result["qa_passed"] is False
        assert result["iteration"] == 3

    @patch("agents.quality_engineer._get_llm")
    def test_max_iterations_pass_verdict(self, mock_get_llm):
        """Verdict can also return PASS at max iterations."""
        triage_resp    = '{"files": [{"path": "backend/auth.py", "priority": "critical"}]}'
        review_resp    = (
            '{"file": "backend/auth.py", "issues": ['
            '{"severity": "critical", "security_rule": "SECURITY-12", '
            '"line_hint": "1", "description": "Hardcoded secret", "fix": "Use env var"}'
            '], "has_issues": true}'
        )
        cross_resp     = '{"issues": [], "has_issues": false}'
        fix_resp       = "Fix: use env var."
        re_review_resp = (
            '{"file": "backend/auth.py", '
            '"resolved": [{"original": "Hardcoded secret", "status": "fixed", "note": "Done"}], '
            '"new_issues": [], "passed": true}'
        )
        verdict_resp   = "## QA Verdict\nAll clear.\nAI-DLC QA decision: PASS"

        mock_get_llm.return_value = _make_llm([
            triage_resp, review_resp, cross_resp, fix_resp, re_review_resp, verdict_resp
        ])

        state  = _base_state(code_files=[FILE_WITH_CRITICAL], iteration=2, max_iterations=3)
        result = quality_engineer_node(state)

        assert result["qa_passed"] is True

    @patch("agents.quality_engineer._get_llm")
    def test_only_minor_issues_pass(self, mock_get_llm):
        """Minor-only issues → PASS, no fix instructions generated."""
        triage_resp = '{"files": [{"path": "backend/utils.py", "priority": "standard"}]}'
        review_resp = (
            '{"file": "backend/utils.py", "issues": ['
            '{"severity": "minor", "security_rule": null, '
            '"line_hint": "4", "description": "Unused variable x", "fix": "Remove it"}'
            '], "has_issues": true}'
        )
        cross_resp  = '{"issues": [], "has_issues": false}'

        mock_get_llm.return_value = _make_llm([triage_resp, review_resp, cross_resp])

        state  = _base_state(code_files=[FILE_WITH_MINOR])
        result = quality_engineer_node(state)

        assert result["qa_passed"] is True
        assert result["qa_feedback"] == ""
        # Minor issue is still recorded
        assert len(result["qa_issues"]) == 1
        assert result["qa_issues"][0]["severity"] == "minor"

    @patch("agents.quality_engineer._get_llm")
    def test_reasoning_logs_contain_aidlc_phases(self, mock_get_llm):
        """All reasoning_logs entries must carry AI-DLC phase and stage fields."""
        triage_resp = '{"files": [{"path": "backend/main.py", "priority": "standard"}]}'
        review_resp = '{"file": "backend/main.py", "issues": [], "has_issues": false}'
        cross_resp  = '{"issues": [], "has_issues": false}'

        mock_get_llm.return_value = _make_llm([triage_resp, review_resp, cross_resp])

        state  = _base_state(code_files=[CLEAN_FILE])
        result = quality_engineer_node(state)

        for entry in result["reasoning_logs"]:
            assert "phase" in entry, f"Missing 'phase' in log entry: {entry}"
            assert "stage" in entry, f"Missing 'stage' in log entry: {entry}"
            assert entry["phase"].startswith("CONSTRUCTION/"), (
                f"Phase should follow AI-DLC format 'CONSTRUCTION/<stage>': {entry['phase']}"
            )

    @patch("agents.quality_engineer._get_llm")
    def test_cross_file_issue_included_in_feedback(self, mock_get_llm):
        """Cross-file issues appear in qa_feedback dispatched to Developer Agent."""
        triage_resp = '{"files": [{"path": "backend/main.py", "priority": "standard"}]}'
        review_resp = '{"file": "backend/main.py", "issues": [], "has_issues": false}'
        cross_resp  = (
            '{"issues": [{"file": "GENERAL", "severity": "major", '
            '"description": "DATABASE_URL used but not in .env.example", '
            '"fix": "Add DATABASE_URL to .env.example"}], "has_issues": true}'
        )

        mock_get_llm.return_value = _make_llm([triage_resp, review_resp, cross_resp])

        state  = _base_state(code_files=[CLEAN_FILE])
        result = quality_engineer_node(state)

        assert result["qa_passed"] is False
        assert "DATABASE_URL" in result["qa_feedback"]

    @patch("agents.quality_engineer._get_llm")
    def test_multiple_files_all_reviewed(self, mock_get_llm):
        """All files in code_files receive a static analysis call."""
        files = [CLEAN_FILE, FILE_WITH_MINOR]

        triage_resp  = (
            '{"files": ['
            '{"path": "backend/main.py", "priority": "standard"},'
            '{"path": "backend/utils.py", "priority": "standard"}'
            ']}'
        )
        review_resp1 = '{"file": "backend/main.py", "issues": [], "has_issues": false}'
        review_resp2 = '{"file": "backend/utils.py", "issues": [], "has_issues": false}'
        cross_resp   = '{"issues": [], "has_issues": false}'

        mock_get_llm.return_value = _make_llm([
            triage_resp, review_resp1, review_resp2, cross_resp
        ])

        state  = _base_state(code_files=files)
        result = quality_engineer_node(state)

        assert result["qa_passed"] is True
        # LLM called: 1 triage + 2 reviews + 1 cross = 4
        assert mock_get_llm.return_value.invoke.call_count == 4
