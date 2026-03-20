# Codebase Guide

This document explains every file in the project: what it does, why it exists, what it connects to, and how it fits into the overall architecture.

---

## File Tree

```
jame-agents/
├── CODEBASE.md                        ← this file
├── pytest.ini                         ← pytest marker registration
├── requirements.txt                   ← Python dependencies
├── .env.example                       ← credentials template
├── state.py                           ← shared data models + pipeline state
├── graph.py                           ← LangGraph workflow (agent wiring)
├── main.py                            ← CLI entry point + artifact writer
├── agents/
│   └── quality_engineer.py            ← Quality Engineer agent (fully implemented)
├── prompts/
│   └── quality_engineer.yaml          ← LLM prompt templates for each QA stage
└── tests/
    ├── __init__.py
    ├── test_quality_engineer.py        ← unit tests (mocked LLM, <1s)
    └── test_qa_integration.py          ← integration tests (real LLM, ~mins)
```

---

## Architecture Overview

This project is a **multi-agent AI software factory** following the
[AI-DLC (AI-Driven Development Life Cycle)](https://github.com/awslabs/aidlc-workflows)
methodology. Agents each own a phase of the software lifecycle. They communicate
through a shared `AgentState` dict that flows through a LangGraph pipeline.

```
User request
    │
    ▼
┌─────────────┐     ┌─────────────┐     ┌──────────────────────┐
│  Architect  │────▶│  Developer  │────▶│  Quality Engineer    │
│  (stub)     │     │  (stub)     │◀────│  (implemented)       │
└─────────────┘     └─────────────┘     └──────────┬───────────┘
   specs, diagrams    code_files             qa_passed?
                           ▲                    │
                           │   fix feedback     │ FAIL + iterations left
                           └────────────────────┘
                                                │ PASS or max iterations
                                                ▼
                                              output/
```

The QA agent is the only fully implemented agent. Architect and Developer are
stubs in `graph.py` awaiting implementation.

---

## Files

---

### `state.py` — Shared Data Models

**Why it exists:** Every agent reads from and writes to the same `AgentState`
dict. This file defines that dict and all the types it carries. It is the
single source of truth for what data flows through the pipeline.

**What it exports:**

| Type | Kind | Purpose |
|------|------|---------|
| `AgentState` | TypedDict | Central pipeline state — passed between all agents |
| `CodeFile` | Pydantic model | One generated source file (path + content + language) |
| `QAIssue` | Pydantic model | One QA finding (file, severity, description) |
| `ReasoningEntry` | TypedDict | One AI-DLC audit trace entry (agent, phase, stage, content) |
| `GeneratedCode` | Pydantic model | Developer output schema (list of CodeFile) |
| `FilePlan` | Pydantic model | File-planning output (list of paths) |
| `SingleFileContent` | Pydantic model | Single file generation output schema |
| `QAResult` | Pydantic model | QA verdict schema (passed bool + issues list) |

**`AgentState` fields:**

```
user_request    str              Raw user prompt (CLI input)
specs           str              Requirements + design (written by Architect)
diagrams        str              Mermaid C4 diagrams (written by Architect)
code_files      list[CodeFile]   Generated source files (written by Developer)
cicd_yaml       str              GitHub Actions workflow YAML
dockerfile      str              Dockerfile content
qa_passed       bool             True if QA approved the code
qa_feedback     str              Fix instructions dispatched to Developer
qa_issues       list[QAIssue]    Structured issues (file, severity, description)
iteration       int              Current fix loop count (starts at 0)
max_iterations  int              Safety cap on the feedback loop (default 3)
reasoning_logs  list[...]        Append-only AI-DLC audit trail (all agents write)
```

**Key design notes:**
- `reasoning_logs` is annotated with `operator.add` — LangGraph merges it by
  appending, never overwriting. This gives an immutable audit trail.
- `_sanitize_path()` strips markdown artifacts (backticks, bullets) from file
  paths before they reach the filesystem.

**Connects to:** everything — all agents import from here.

---

### `graph.py` — Pipeline Wiring

**Why it exists:** Defines the LangGraph `StateGraph` that connects agents in
sequence and handles the QA feedback loop. This is where the pipeline topology
lives, separate from any agent's business logic.

**What it exports:** `build_graph()` → a compiled `StateGraph` ready to invoke.

**Pipeline topology:**

```
START → architect_node → developer_node → quality_engineer_node
                               ▲                    │
                               │    _route_after_qa │
                               │                    ▼
                               │         qa_passed=True ──→ END
                               │         iteration≥max  ──→ END
                               └──────── otherwise (FAIL)
```

**`_route_after_qa()` routing logic:**
- `qa_passed=True` → `END`
- `iteration >= max_iterations` → `END` (even if FAIL — safety exit)
- otherwise → back to `developer_node` with `qa_feedback` in state

**Key design note:** The iteration counter is incremented *inside*
`quality_engineer_node` before returning. The router reads the already-
incremented value to decide whether to loop or exit.

**Connects to:** `state.py`, `agents/quality_engineer.py`.

---

### `main.py` — CLI Entry Point

**Why it exists:** Provides the user-facing entry point and handles everything
outside the pipeline: loading credentials, constructing the initial state,
invoking the graph, persisting artifacts to disk, and printing a summary.

**Key functions:**

| Function | Purpose |
|----------|---------|
| `main()` | Load `.env`, take user input, run graph, call save + print |
| `save_artifacts(state)` | Write all pipeline outputs to `output/` |
| `print_reasoning_summary(logs)` | Pretty-print AI-DLC trace to console |
| `_sanitize_file_path(path)` | Strip markdown artifacts before writing files |

**Output directory structure:**

```
output/
├── specifications.md               ← state.specs
├── c4_diagrams.md                  ← state.diagrams
├── reasoning_trace.json            ← state.reasoning_logs (full audit trail)
├── qa_issues.json                  ← state.qa_issues (structured issue list)
└── project/
    ├── <generated source files>    ← state.code_files (respecting subdirectories)
    ├── .github/workflows/ci.yml    ← state.cicd_yaml
    ├── Dockerfile                  ← state.dockerfile
    └── README_WARNING.md           ← written only if qa_passed=False at max iterations
```

**Connects to:** `graph.py`, `state.py`.

---

### `agents/quality_engineer.py` — Quality Engineer Agent

**Why it exists:** Implements the AI-DLC CONSTRUCTION phase / Build and Test
stage. It reviews generated code against the spec and security rules, produces
fix instructions for the Developer, and issues a final PASS/FAIL verdict.

This is the only fully implemented agent in the project.

**What it exports:**

| Symbol | Purpose |
|--------|---------|
| `quality_engineer_node(state)` | LangGraph node — main entry point |
| `_triage(llm, specs, files)` | Classify files by risk priority |
| `_analyse_file(llm, specs, file, priority)` | Per-file static analysis |
| `_cross_file_check(llm, specs, per_file_results)` | Cross-file consistency check |
| `_generate_fix_instructions(llm, specs, file, issues)` | Patch or rewrite brief |
| `_re_review_file(llm, file, original_issues)` | Verify fixes on final iteration |
| `_issue_verdict(llm, re_review_results)` | Final PASS/FAIL report |
| `_collect_qa_issues(per_file, cross_file)` | Merge all issues into list[QAIssue] |

**Execution flow inside `quality_engineer_node`:**

```
[TRIAGE]    Classify each file: critical / important / standard
    │
    ▼
[REVIEW]    Analyse each file in priority order (critical first)
    │         Check: correctness, security rules, spec compliance, code quality
    │
    ▼
[CROSS]     Check cross-file consistency
    │         (import/export mismatches, missing env vars, shared models)
    │
    ├─ No critical/major issues?
    │       └─▶ immediate PASS (skip fix loop)
    │
    ▼
[FIX]       Generate fix instructions per file with issues
    │         < 3 critical issues → patch instructions (numbered steps)
    │         ≥ 3 critical issues → full rewrite brief (escalation)
    │
    ├─ iteration + 1 < max_iterations?
    │       └─▶ return FAIL + qa_feedback → Developer loops
    │
    ▼  (final iteration only)
[RE-REVIEW] Re-check each file: resolved / unresolved / new issues introduced
    │
    ▼
[VERDICT]   Emit final "AI-DLC QA decision: PASS/FAIL" report
```

**Security rules enforced:**

| Rule | Checks for |
|------|-----------|
| SECURITY-01 | Encryption at rest / in transit |
| SECURITY-03 | No secrets in application logs |
| SECURITY-04 | HTTP security headers present |
| SECURITY-05 | Input validation at all API boundaries |
| SECURITY-08 | Authentication + authorisation on all routes (IDOR prevention) |
| SECURITY-09 | Security hardening / no misconfigurations |
| SECURITY-12 | No hardcoded credentials — use env vars |
| SECURITY-15 | Generic error responses — no stack traces leaked |

**LLM configuration:**
- Provider: Snowflake Cortex (OpenAI-compatible endpoint)
- Model: `deepseek-r1`
- Temperature: `0.3` (low — more deterministic for reviews)
- Max tokens: `4096`
- Thinking blocks: `<think>...</think>` extracted and logged separately

**Robustness patterns:**
- `_strip_thinking()` — separates chain-of-thought from structured output
- `_parse_json_response()` — extracts JSON from markdown fences; falls back to
  an empty/default dict on parse failure (never crashes on bad LLM output)
- `_maybe_compress()` — compresses rolling memory context when it exceeds 5000
  chars to stay within token limits

**Connects to:** `state.py`, `prompts/quality_engineer.yaml`.

---

### `prompts/quality_engineer.yaml` — LLM Prompt Templates

**Why it exists:** Keeps all LLM instructions out of Python code. Each stage
has its own prompt with explicit JSON response schemas, so the agent code only
handles logic — not prompt text.

**Prompt keys:**

| Key | Used by | Purpose |
|-----|---------|---------|
| `triage` | `_triage()` | Classify files into critical/important/standard |
| `review.static_analysis` | `_analyse_file()` | Per-file correctness + security review |
| `review.cross_file` | `_cross_file_check()` | Cross-file consistency check |
| `fix.patch_instructions` | `_generate_fix_instructions()` | Numbered patch steps |
| `fix.full_rewrite` | `_generate_fix_instructions()` | Rewrite brief (escalation path) |
| `validation.re_review` | `_re_review_file()` | Verify fixes were actually applied |
| `validation.verdict` | `_issue_verdict()` | Final PASS/FAIL audit report |
| `memory.compress` | `_maybe_compress()` | Summarise rolling context |

**Connects to:** `agents/quality_engineer.py` (loaded once at module import via
`_load_prompts()`).

---

### `pytest.ini` — Pytest Configuration

**Why it exists:** Registers the `fast` marker so pytest does not emit
"unknown mark" warnings. Without this file, running `-m fast` still works but
produces a warning on every run.

**Content:**
```ini
[pytest]
markers =
    fast: minimal smoke tests — single LLM call, run these first to check the agent works
```

**Connects to:** `tests/test_qa_integration.py` (`@pytest.mark.fast`).

---

### `requirements.txt` — Python Dependencies

| Package | Why |
|---------|-----|
| `langgraph>=0.2.0` | StateGraph pipeline orchestration |
| `langchain-openai>=0.2.0` | `ChatOpenAI` wrapper (used for Snowflake Cortex) |
| `langchain-core>=0.3.0` | `HumanMessage` and base abstractions |
| `python-dotenv>=1.0.0` | Load credentials from `.env` |
| `pydantic>=2.0.0` | `CodeFile`, `QAIssue`, and other models |
| `pyyaml>=6.0` | Parse `prompts/quality_engineer.yaml` |
| `pytest>=8.0.0` | Test runner |
| `pytest-mock>=3.14.0` | Mock fixtures for unit tests |

---

### `.env.example` — Credentials Template

**Why it exists:** Documents which environment variables are required without
committing real credentials. Copy to `.env` and fill in values before running.

**Required variables:**

```
SNOWFLAKE_API_KEY         API key for Snowflake Cortex (used as openai_api_key)
SNOWFLAKE_API_BASE        Cortex endpoint URL (used as openai_api_base)
```

**Connects to:** `agents/quality_engineer.py` (`_get_llm()`), `main.py`.

---

### `tests/test_quality_engineer.py` — Unit Tests (Mocked LLM)

**Why it exists:** Tests the agent's *logic* — iteration counter, early PASS
on minor-only issues, escalation to rewrite brief, JSON fallback, thinking
block stripping — completely independently of the LLM. No API key needed.
Runs in under one second.

**What it tests:**

| Class | Tests |
|-------|-------|
| `TestTriage` | File classification, unlisted-file defaults, malformed JSON, thinking blocks |
| `TestAnalyseFile` | Clean files, critical/major/minor issues, JSON fallback |
| `TestCrossFileCheck` | No issues, missing env vars, import mismatches |
| `TestGenerateFixInstructions` | Patch path, rewrite escalation (≥3 critical), boundary at 2 critical |
| `TestReReviewFile` | All fixed, unresolved, new issue introduced |
| `TestIssueVerdict` | PASS, FAIL, exact marker string required |
| `TestCollectQaIssues` | Per-file + cross-file merge, empty inputs, file assignment |
| `TestQualityEngineerNode` | Full node: no files, clean code, critical issue, iteration counter, max iterations (FAIL+PASS), minor-only, reasoning logs, cross-file feedback |

**Mocking pattern:** `@patch("agents.quality_engineer._get_llm")` replaces the
LLM with a `MagicMock` whose `.invoke.side_effect` returns a pre-set sequence
of response strings.

**When to run:**
```bash
pytest tests/test_quality_engineer.py -v
```
Run this after every code change to `quality_engineer.py`. Fast enough to keep
in a pre-commit hook.

**Connects to:** `state.py`, `agents/quality_engineer.py`.

---

### `tests/test_qa_integration.py` — Integration Tests (Real LLM)

**Why it exists:** Tests that the LLM actually detects specific bugs in
real-looking code — things a mocked test cannot verify. Requires
`SNOWFLAKE_API_KEY` in `.env`; skipped automatically if the key is absent.

**Test hierarchy by speed:**

| Marker | Class | What it tests | ~Time |
|--------|-------|---------------|-------|
| `@pytest.mark.fast` | `TestSmoke` | Single file → FAIL with critical issue | ~20s |
| (none) | `TestCleanCode` | Clean router + models → PASS | ~30s |
| (none) | `TestHardcodedSecret` | Detects `SECRET_KEY = "..."` + feedback mentions env vars | ~40s |
| (none) | `TestMissingAuth` | Detects missing `Depends(get_current_user)` + SQL injection | ~40s |
| (none) | `TestLeakingErrors` | Detects `traceback.format_exc()` in HTTP response | ~40s |
| (none) | `TestEmptyStub` | Detects `# TODO: implement` stub files | ~20s |
| (none) | `TestMultipleFilesWithMixedQuality` | Mixed files, reasoning log structure, all bugs at once | ~90s |

**Progress output:** Every test calls `_run_timed()` which prints to `stderr`
(visible even without `-s`):
```
[QA-TEST] START  smoke:hardcoded_secret  [1 file(s): auth.py]  — making LLM calls, please wait...
[QA-TEST] DONE   smoke:hardcoded_secret  →  FAIL  |  2 issue(s)  |  18.3s
```

**Sample files used as fixtures:**

| Variable | File | Bug planted |
|----------|------|------------|
| `FILE_HARDCODED_SECRET` | `backend/auth.py` | `SECRET_KEY = "super_secret_password_123"` — SECURITY-12 |
| `FILE_MISSING_AUTH` | `backend/routers/tasks_insecure.py` | No `Depends(get_current_user)` + raw SQL concat — SECURITY-08, SECURITY-05 |
| `FILE_LEAKING_ERRORS` | `backend/services/task_service.py` | `traceback.format_exc()` in HTTP response + no title validation — SECURITY-15, SECURITY-05 |
| `FILE_EMPTY_STUB` | `backend/models.py` | `# TODO: implement\npass` — unimplemented critical file |
| `CLEAN_ROUTER` | `backend/routers/tasks.py` | None — should PASS |
| `CLEAN_MODELS` | `backend/models.py` | None — should PASS |

**When to run:**
```bash
# Quick sanity check (single LLM call):
pytest tests/test_qa_integration.py -v -m fast

# Full suite:
pytest tests/test_qa_integration.py -v
```

**Connects to:** `state.py`, `agents/quality_engineer.py`.

---

## Running Tests

```bash
# Unit tests — no API key, <1s
pytest tests/test_quality_engineer.py -v

# Integration smoke — 1 LLM call, ~20s
pytest tests/test_qa_integration.py -v -m fast

# Integration full — all LLM calls, ~5-10min
pytest tests/test_qa_integration.py -v

# Everything
pytest -v
```

---

## How to Add a New Agent

1. **Create `agents/<name>.py`** — implement `<name>_node(state: AgentState) -> dict`.
   The function reads from `state` and returns only the keys it writes.
2. **Add prompts** to `prompts/<name>.yaml` if the agent uses an LLM.
3. **Wire it into `graph.py`** — add a node and the appropriate edges/routing.
4. **Update `state.py`** if the agent needs new state fields.
5. **Write unit tests** in `tests/test_<name>.py` with mocked LLM calls.
6. **Write integration tests** in `tests/test_<name>_integration.py` with real
   LLM calls and planted bugs/scenarios.
