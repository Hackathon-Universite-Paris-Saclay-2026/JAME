# JAME Workflow — Setup & Run Guide

## Architecture

```
┌─────────────────────────────────────────────┐
│  VS Code Extension (TypeScript)             │
│  - Side panel UI (chat-like interface)      │
│  - Sends requests to backend API            │
│  - Streams events via WebSocket             │
└────────────────────┬────────────────────────┘
                     │
                     │ HTTP/WebSocket
                     ↓
┌─────────────────────────────────────────────┐
│  Backend Service (Python/FastAPI)           │
│  - REST API for run management              │
│  - WebSocket event streaming                │
│  - LangGraph orchestrator                   │
└─────────────────────┬───────────────────────┘
                      │
                      ├─── Architect Agent ──→ Load prompts/solution_architect.yaml
                      ├─── Developer Agent ──→ Load prompts/software_engineer.yaml
                      ├─── Delivery Agent ───→ Load prompts/delivery_engineer.yaml
                      └─── QA Agent ─────────→ Load prompts/quality_engineer.yaml
                           │
                           ↓
                    Snowflake Cortex
                    (DeepSeek R1 LLM)
```

## Prerequisites

- Python 3.11+
- Node.js + npm
- Snowflake API credentials (or OpenAI fallback)
- VS Code with Extension Development Host support

## Backend Setup

### 1. Install Python Dependencies

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy and edit the template
cp .env.example .env

# Edit .env with your credentials:
# SNOWFLAKE_API_KEY=your_key
# SNOWFLAKE_API_BASE=https://your-snowflake-region.execute-api.amazonaws.com
```

If you don't have Snowflake, the fallback uses `OPENAI_API_KEY`.

### 3. Start Backend API

```bash
source .venv/bin/activate
python run_api.py
```

The API will start on `http://localhost:8000`

Test it:
```bash
curl http://localhost:8000/health
# Should return: {"status":"ok"}
```

## Extension Setup

### 1. Install Node Dependencies

```bash
cd extension
npm install
```

### 2. Compile TypeScript

```bash
npm run compile
```

### 3. Run in Development Mode

```bash
code --extensionDevelopmentPath="$PWD"
```

VS Code will open a new window with the extension loaded in the sidebar.

## Usage

1. **Open the JAME Workflow panel** in the VS Code sidebar (should appear automatically)
2. **Enter a product description** in the text input:
   ```
   Build a REST API for a todo app with PostgreSQL, JWT auth, and FastAPI
   ```
3. **Click "Build"** to start the orchestration
4. **Watch the chat feed** as agents work:
   - **Architect** analyzes and designs the system
   - **Developer** generates code files
   - **Delivery** creates CI/CD pipelines
   - **QA** validates the output

## File Locations

- **Root prompts**: `/path/to/JAME/prompts/*.yaml`
- **Backend source**: `backend/` (agents, API layer, state management)
- **Extension source**: `extension/src/` (TypeScript panel implementation)
- **Shared contracts**: `contracts/` (OpenAPI, WebSocket schema)
- **Documentation**: `docs/IBM.md` (hackathon requirements)

## API Endpoints

### REST

- `POST /runs` — Create a new orchestration run
- `GET /runs/{run_id}` — Get run status and artifacts
- `GET /health` — Health check

### WebSocket

- `WS /ws/runs/{run_id}` — Stream run events

## Configuration

### Extension Settings

In VS Code settings (`settings.json`):

```json
{
  "jameWorkflow.backendUrl": "http://localhost:8000"
}
```

To use a remote backend, change this URL.

## Troubleshooting

### Backend not starting

```bash
# Make sure dependencies are installed
pip install -r requirements.txt

# Check Python version
python --version  # Should be 3.11+

# Verify Snowflake/OpenAI credentials
echo $SNOWFLAKE_API_KEY  # or $OPENAI_API_KEY
```

### Extension not appearing

- Close all VS Code windows
- Run the dev host again: `code --extensionDevelopmentPath="$PWD"`
- Open the JAME Workflow panel from the activity bar

### WebSocket connection errors

- Ensure backend is running on correct port (8000)
- Check extension settings for correct `backendUrl`
- Look at backend logs for errors

### LLM not responding

- Verify API credentials in `.env`
- Check Snowflake/OpenAI API connectivity
- Monitor backend console for rate limit or auth errors

## Building for Production

### Package Extension

```bash
cd extension
npm install -g vsce
vsce package
```

This creates `jame-workflow-extension-0.0.1.vsix` for distribution.

### Deploy Backend

```bash
# Create Docker image
docker build -t jame-workflow-backend .

# Run container
docker run -p 8000:8000 \
  -e SNOWFLAKE_API_KEY=$SNOWFLAKE_API_KEY \
  -e SNOWFLAKE_API_BASE=$SNOWFLAKE_API_BASE \
  jame-workflow-backend
```

## Next Steps

- Implement streaming output (real-time artifact previews)
- Add run history and replay functionality
- Build artifact explorer (download generated code, diagrams, etc.)
- Integration with GitHub to auto-push generated projects
- Support for multiple language targets (Node.js, Go, Rust)
