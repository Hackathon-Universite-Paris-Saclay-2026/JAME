# JAME — Multi-Agent Software Factory

A production-ready multi-agent system for automated full-stack software generation. Powered by LangGraph, DeepSeek R1 (Snowflake Cortex), and a professional VS Code side-panel interface.

## Features

- **AI-DLC Pipeline** — Architect → Developer → QA (loop) → DevOps agents following the [AI-Driven Development Life Cycle](https://github.com/awslabs/aidlc-workflows)
- **AIDLC-Inspired Developer** — Four-phase workflow: Functional Design → File Planning → Chunked Code Generation → Self-Validation
- **Real-Time Streaming** — Watch agents work live via WebSocket event feed in VS Code
- **Professional UI** — Clean, Copilot-inspired side-panel chat interface
- **REST + WebSocket API** — Full async run management with cancellation support
- **Instant Cancel** — Stop any run immediately; backend unblocks without waiting for in-flight LLM calls
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
2. Describe your app: _"Build a REST API for task management with PostgreSQL"_
3. Click **Build**
4. Watch the Architect, Developer, QA, and DevOps agents work in real-time
5. Preview, diff, and save generated files directly from the panel

## Architecture

```
VS Code Extension (TypeScript)
     ↓ HTTP / WebSocket
FastAPI Backend (uvicorn)
     ↓ LangGraph pipeline
  ┌──────────────────────────────────┐
  │  Architect → Developer → QA loop │
  │              ↓ (pass)            │
  │            DevOps                │
  └──────────────────────────────────┘
        ↓
  Snowflake Cortex (DeepSeek R1)
```

### Agent pipeline

| Agent | Role |
|-------|------|
| **Architect** | Classifies scope, generates C4 diagrams and structured specs |
| **Developer** | Functional design → file plan → chunked code gen → self-validation |
| **Quality Engineer** | Triage → static review → cross-file check → fix briefs → verdict |
| **DevOps** | Decides CI/CD need, generates GitHub Actions workflows and Docker artifacts |

### API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Backend liveness check |
| `POST` | `/runs` | Start a new pipeline run (returns `run_id`) |
| `GET` | `/runs/{run_id}` | Get run status and artifacts |
| `POST` | `/runs/{run_id}/cancel` | Request immediate cancellation |
| `WebSocket` | `/ws/runs/{run_id}` | Stream real-time events |

### Extension command IDs

The smart search-and-replace command intentionally keeps the namespaced ID
`jameWorkflow.searchReplaceSmart`.

- This preserves ownership clarity in multi-extension workspaces.
- UI label remains: **JAME: Smart Search & Replace**.
- Keybindings and automation should target the namespaced command ID.

### WebSocket events

| Event | Description |
|-------|-------------|
| `run_started` | Pipeline has begun |
| `agent_update` | A node emitted a reasoning log entry |
| `file_generated` | A single file was written (includes content) |
| `files_ready` | All files for the current iteration are on disk |
| `run_completed` | Pipeline succeeded — includes `project_dir` and `generated_files` |
| `run_failed` | Pipeline error — includes error message |
| `run_cancelled` | User cancelled the run |

## Key Files

```
├── main.py                        # FastAPI entry point
├── config.py                      # Typed settings from .env
├── cancel_token.py                # Thread-safe cancellation token
├── api/
│   ├── app.py                     # FastAPI app + all endpoints
│   ├── service.py                 # OrchestratorService (graph → WebSocket bridge)
│   ├── job_store.py               # In-memory pub-sub run store
│   └── models.py                  # Pydantic request/response models
├── graph/
│   ├── graph.py                   # LangGraph workflow definition
│   ├── state.py                   # AgentState TypedDict
│   └── nodes/
│       ├── architect_node.py      # Scope classification + C4 design
│       ├── developer_node.py      # AIDLC 4-phase code generation
│       ├── qa_node.py             # AI-DLC quality assurance
│       └── devops_node.py         # CI/CD + Docker generation
├── integrations/
│   └── cortex.py                  # Snowflake Cortex LLM client factory
├── utils/
│   └── node.py                    # save_artifacts, git init, shared utilities
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
# Start a run
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"user_request": "Build a task management REST API", "max_iterations": 3}'

# Check status
curl http://localhost:8000/runs/{run_id}

# Cancel
curl -X POST http://localhost:8000/runs/{run_id}/cancel

# Stream events (WebSocket)
# ws://localhost:8000/ws/runs/{run_id}
```

## Design Philosophy

- **AI-DLC first** — Every agent follows the AI-Driven Development Life Cycle methodology
- **Type-safe** — TypeScript + Python with strict typing throughout
- **Observable** — All reasoning logged and streamed live to the VS Code UI
- **Non-blocking** — API mode auto-detects TTY absence; no `input()` calls ever block the server
- **Cancellable** — Cancel is immediate: the async loop is unblocked without waiting for the LLM
- **Reproducible** — Every generated project is git-initialised at generation time
