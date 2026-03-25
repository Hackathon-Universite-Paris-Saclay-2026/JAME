# JAME — Just Another Model-Driven Engineer

A production-ready multi-agent system for automated full-stack software generation. Powered by LangGraph, DeepSeek R1 (Snowflake Cortex), and a professional VS Code side-panel interface.

## Features

- **AI-DLC Pipeline** — Architect → Developer → QA (loop) → DevOps agents following the [AI-Driven Development Life Cycle](https://github.com/awslabs/aidlc-workflows)
- **AIDLC-Inspired Developer** — Four-phase workflow: Functional Design → File Planning → Chunked Code Generation → Self-Validation
- **Three Execution Modes** — Junior (learning stubs), Senior (human-in-the-loop), Expert (fully autonomous)
- **Real-Time Streaming** — Watch agents work live via WebSocket event feed in VS Code; files stream per-file as they are generated
- **Professional UI** — Clean, Copilot-inspired side-panel chat interface with inline diff editor
- **REST + WebSocket API** — Full async run management with cancellation support
- **Instant Cancel** — Stop any run immediately; backend unblocks without waiting for in-flight LLM calls
- **Human-in-the-Loop** — Specs approval gate, mid-pipeline instruction injection, and QA tool run/skip decisions (senior mode)
- **Multi-Instance** — Multiple VS Code windows each spawn their own backend on a free port
- **Snowflake Cortex** — Uses DeepSeek R1 via Snowflake Cortex API
- **Git Scaffold** — Every generated project is automatically git-initialised with an initial commit

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- A Snowflake account with Cortex access and a valid JWT token

### Backend

```bash
make create-venv
make install-deps
cp .env.example .env
# Edit .env with your Snowflake credentials
make run
```

The API runs on `http://localhost:8000`.

### VS Code Extension

```bash
make extension
code --extensionDevelopmentPath="$PWD/extension"
```

The JAME Workflow panel appears in the Activity Bar automatically.

> **Auto-start:** The extension spawns the backend automatically when the panel opens — no need to run `make run` manually.

### Usage

1. Open the **JAME Workflow** panel in the VS Code Activity Bar
2. Select a mode: **Junior**, **Senior**, or **Expert**
3. Describe your app: _"Make me a todo app with priority and due date"_
4. Click **Build**
5. Watch the Architect, Developer, QA, and DevOps agents work in real-time
6. In Senior mode: review and approve specs before code generation begins
7. Preview, diff, and save generated files directly from the panel

## Execution Modes

| Mode | Behaviour |
|------|-----------|
| **Junior** | Generates a complete reference solution, then produces exercise stubs with TODOs for learning |
| **Senior** | Human-in-the-loop: specs approval gate before code generation, QA review checkpoints, mid-pipeline instruction injection |
| **Expert** | Fully autonomous: enterprise-grade output, no human prompts, highest code quality |

## Architecture

```
VS Code Extension (TypeScript)
     ↓ HTTP / WebSocket
FastAPI Backend (uvicorn)
     ↓ LangGraph pipeline
  ┌──────────────────────────────────────────────┐
  │  Architect → Developer → QA loop → DevOps   │
  │  (Junior mode: → Exercise Generator)         │
  └──────────────────────────────────────────────┘
        ↓
  Snowflake Cortex (DeepSeek R1)
```

### Agent Pipeline

| Agent | Role |
|-------|------|
| **Architect** | Classifies scope, runs interrogation rounds, generates C4 diagrams and structured specs. Senior mode: waits for human approval before proceeding. |
| **Developer** | Functional design → layer-ordered file plan → chunked code generation → self-validation. Consumes senior prompt queue on each iteration. |
| **Quality Engineer** | AI-DLC: Triage → per-file static review → cross-file check → fix instructions → re-review → verdict. Ruff auto-fix before LLM retry. |
| **DevOps** | Decides CI/CD need, generates GitHub Actions workflows and Docker artifacts. |
| **Exercise Generator** | Junior mode only: strips reference solution into stubs with TODOs, preserving learning objectives. |

### Pipeline Flow

```
user_request → Architect ──(specs approved?)──► Developer → QA ─┐
                                                     ↑           │ FAIL
                                                     └───────────┘
                                                                 │ PASS (or max_iterations reached)
                                                                 ▼
                                                              DevOps
                                                                 │
                                                    (junior) ────► Exercise Generator
                                                                 │
                                                                END
```

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Backend liveness check (returns `instance_id`) |
| `POST` | `/runs` | Start a new pipeline run — accepts `user_request`, `max_iterations`, `mode` |
| `GET` | `/runs/{run_id}` | Get run status and artifacts |
| `POST` | `/runs/{run_id}/clarify` | Answer an architect clarification question |
| `POST` | `/runs/{run_id}/approve-specs` | Approve or request revision of architect specs (senior mode) |
| `POST` | `/runs/{run_id}/queue-prompt` | Inject an instruction for the developer's next iteration (senior mode) |
| `POST` | `/runs/{run_id}/tool-response` | Approve or skip a QA tool call (`run` / `skip`) |
| `POST` | `/runs/{run_id}/cancel` | Request immediate cancellation |
| `GET` | `/runs/{run_id}/exercise` | Fetch exercise stubs and learning objectives (junior mode) |
| `POST` | `/runs/{run_id}/submit` | Submit junior solution for validation |
| `GET` | `/runs/{run_id}/hint` | Unlock the next progressive hint (junior mode) |
| `WebSocket` | `/ws/runs/{run_id}` | Stream real-time events |

### Run Lifecycle States

| Status | Meaning |
|--------|---------|
| `pending` | Run created, pipeline not yet started |
| `running` | Pipeline actively executing |
| `awaiting_specs_review` | Paused — waiting for human to approve or revise specs |
| `awaiting_submission` | Junior mode — waiting for learner to submit solution |
| `succeeded` | Pipeline completed successfully |
| `failed` | Pipeline error |
| `cancelled` | Run cancelled by user |

### WebSocket Events

| Event | Description |
|-------|-------------|
| `run_started` | Pipeline has begun |
| `agent_update` | A node emitted a reasoning log entry |
| `architect_done` | Architect finished — payload includes `specs`, `diagrams`, `scope` |
| `file_generated` | A single file was generated (streamed in real-time; includes `path`, `content`, `language`) |
| `files_ready` | All files for the current iteration are on disk |
| `exercise_ready` | Junior mode: exercise stubs are ready for the learner |
| `clarification_request` | Architect is asking a clarification question |
| `specs_review_request` | Senior mode: architect is waiting for specs approval (payload includes full specs) |
| `iteration_review_request` | Senior mode: QA found issues and is waiting for human proceed/instruction decision |
| `tool_call` | QA wants to execute a tool — human must respond via `/tool-response` |
| `prompt_queued` | A developer instruction was successfully queued |
| `run_completed` | Pipeline succeeded — includes `project_dir` and `generated_files` |
| `run_failed` | Pipeline error — includes error message |
| `run_cancelled` | User cancelled the run |

### Extension Command IDs

The smart search-and-replace command intentionally keeps the namespaced ID
`jameWorkflow.searchReplaceSmart`.

- This preserves ownership clarity in multi-extension workspaces.
- UI label remains: **JAME: Smart Search & Replace**.
- Keybindings and automation should target the namespaced command ID.

## Key Files

```
├── main.py                        # FastAPI entry point
├── config.py                      # Typed settings from .env
├── cancel_token.py                # Thread-safe cancellation token
├── api/
│   ├── app.py                     # FastAPI app + all endpoints
│   ├── service.py                 # OrchestratorService (graph → WebSocket bridge)
│   ├── job_store.py               # In-memory pub-sub run store + tool/specs futures
│   └── models.py                  # Pydantic request/response models
├── graph/
│   ├── graph.py                   # LangGraph workflow definition
│   ├── state.py                   # AgentState TypedDict
│   └── nodes/
│       ├── architect_node.py      # Scope classification + C4 design + specs approval
│       ├── developer_node.py      # Layer-ordered code generation + ruff autofix
│       ├── qa_node.py             # AI-DLC quality assurance (triage → verdict)
│       ├── devops_node.py         # CI/CD + Docker generation
│       └── exercise_generator_node.py  # Junior mode: strips solution into stubs
├── integrations/
│   └── cortex.py                  # Snowflake Cortex LLM client factory
├── utils/
│   └── node.py                    # save_artifacts, git init, mode preambles, shared utilities
├── prompts/
│   ├── solution_architect.yaml    # Architect prompts
│   ├── software_engineer.yaml     # Developer prompts + per-file hints
│   ├── quality_engineer.yaml      # QA prompts (AI-DLC triage/review/verdict)
│   └── delivery_engineer.yaml     # DevOps prompts
├── extension/
│   └── src/
│       ├── extension.ts           # VS Code activation + command registration
│       └── view.ts                # JameViewProvider — full side-panel UI
└── test/
    └── test_generate.py           # API endpoint test suite
```

## Configuration

### Backend — `.env`

```env
# Snowflake Cortex (required)
SNOWFLAKE_API_KEY=your_jwt_token
SNOWFLAKE_API_BASE=https://<account>.snowflakecomputing.com/api/v2/cortex/v1

# Server (optional, defaults shown)
JAME_HOST=127.0.0.1
JAME_PORT=8000
```

> **JWT expiry:** Snowflake JWT tokens expire after ~1 hour. Regenerate and update `.env` if you see `401` errors.

### Extension — VS Code settings

```json
{
  "jameWorkflow.backendUrl": "http://localhost:8000"
}
```

## Development

```bash
make test                  # Run pytest suite
make format                # Format with Ruff
make lint-with-auto-fix    # Lint + auto-fix with Ruff
make extension             # Recompile TypeScript extension
```

## API Quick Reference

```bash
# Start a run (senior mode)
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"user_request": "Build a task management REST API", "max_iterations": 3, "mode": "senior"}'

# Check status
curl http://localhost:8000/runs/{run_id}

# Approve specs (senior mode)
curl -X POST http://localhost:8000/runs/{run_id}/approve-specs \
  -H "Content-Type: application/json" \
  -d '{"action": "approve"}'

# Inject instruction for next developer iteration
curl -X POST http://localhost:8000/runs/{run_id}/queue-prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Use async SQLAlchemy for all database calls"}'

# Respond to a QA tool call
curl -X POST http://localhost:8000/runs/{run_id}/tool-response \
  -H "Content-Type: application/json" \
  -d '{"tool_call_id": "<id from event>", "action": "run"}'

# Cancel
curl -X POST http://localhost:8000/runs/{run_id}/cancel

# Stream events (WebSocket)
# ws://localhost:8000/ws/runs/{run_id}
```

## Design Philosophy

- **AI-DLC first** — Every agent follows the AI-Driven Development Life Cycle methodology
- **Type-safe** — TypeScript + Python with strict typing throughout
- **Observable** — All reasoning logged and streamed live to the VS Code UI; files stream per-file in real time
- **Non-blocking** — API mode auto-detects TTY absence; no `input()` calls ever block the server
- **Cancellable** — Cancel is immediate: the async loop is unblocked without waiting for the LLM; specs review futures are also cancelled
- **Mode-aware** — All agents adapt behaviour (code quality, autonomy, human gates) to the selected mode
- **Reproducible** — Every generated project is git-initialised at generation time
