# JAME Workflow

A production-ready multi-agent system for automated software generation. Powered by LangGraph, DeepSeek (Snowflake), and a professional VS Code interface.

## Features

✓ **Multi-Agent Orchestration** — Architect, Developer, Delivery, QA agents work in sequence  
✓ **Real-Time Streaming** — Watch agents work via WebSocket event feed  
✓ **Professional UI** — Clean, Copilot-inspired side-panel chat interface  
✓ **REST + WebSocket API** — Full async job management with event streaming  
✓ **Prompt-Driven** — Agents load behavior from YAML prompt files  
✓ **Snowflake Integration** — Uses DeepSeek R1 via Snowflake Cortex (or OpenAI fallback)  
✓ **Production Architecture** — Type-safe Python backend, compiled TypeScript extension  

## Quick Start

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Snowflake or OpenAI credentials
python run_api.py
```

The API runs on `http://localhost:8000`

### Extension

```bash
cd extension
npm install && npm run compile
code --extensionDevelopmentPath="$PWD"
```

The extension loads in the sidebar automatically.

### Usage

1. Open JAME Workflow panel in VS Code
2. Describe your app: _"Build a REST API for task management with PostgreSQL"_
3. Click **Build**
4. Watch agents architect, code, deliver, and validate in real-time

## Architecture

```
VS Code Extension (TypeScript)
     ↓ HTTP/WebSocket
FastAPI Backend + LangGraph
     ↓ Parallel Agents
  ┌──┴──┬──────┬─────────┐
  ↓     ↓      ↓         ↓
Architect Developer Delivery QA
  ↓     ↓      ↓         ↓
  └─────┴──────┴─────────┘
        ↓
 Snowflake DeepSeek
    (or OpenAI)
```

## Key Files

- `backend/graph.py` — LangGraph orchestrator
- `backend/api/app.py` — FastAPI REST + WebSocket
- `backend/agents/architect.py` — Real agent implementations
- `extension/src/view.ts` — Professional side-panel UI
- `contracts/` — OpenAPI + WebSocket schemas
- `prompts/` — Agent behavior definitions (from root)

## Configuration

### Backend

Edit `backend/.env`:
```env
SNOWFLAKE_API_KEY=your_key
SNOWFLAKE_API_BASE=https://api.snowflakecomputing.com/cortex
```

Or use OpenAI:
```env
OPENAI_API_KEY=sk-...
```

### Extension

In VS Code settings:
```json
{
  "jameWorkflow.backendUrl": "http://localhost:8000"
}
```

## Full Setup Guide

See [SETUP.md](./SETUP.md) for:
- Detailed prerequisites
- Troubleshooting
- Production deployment
- Docker containerization

## API Reference

### Create Run
```bash
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{
    "user_request": "Build a task app",
    "max_iterations": 3
  }'
```

### Get Run Status
```bash
curl http://localhost:8000/runs/{run_id}
```

### Stream Events
```bash
# WebSocket
ws://localhost:8000/ws/runs/{run_id}
```

## Design Philosophy

- **Professional first** — Clean UI inspired by production tools (Copilot, GitHub)
- **Type-safe** — TypeScript + Python with strict typing
- **Contract-driven** — REST + WebSocket specs versioned separately
- **Prompt-based** — Agent behavior fully externalized in YAML
- **Real LLM calls** — No mock responses; actual orchestration
- **Observable** — All reasoning logged and streamed to UI

## Status

✓ Backend orchestrator working  
✓ VS Code extension UI complete  
✓ Professional design applied  
✓ Prompt loading functional  
✓ Agent nodes wired  

## Next Steps

- [ ] Stream partial artifacts (code preview)
- [ ] Add run history UI
- [ ] Implement artifact download
- [ ] GitHub push integration
- [ ] Support more languages
