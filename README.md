# ATLAS

**Autonomous Task & Lifecycle Agent System**

ATLAS is an autonomous operations agent built for the [Google All Things Agentic Hackathon 2026](https://google.github.io/adk-docs/). Instead of requiring step-by-step instructions, ATLAS accepts a high-level goal and autonomously plans, executes, and reports on the work.

> **Round 1 focus:** Prove the core autonomous mission lifecycle with a working vertical slice — create a mission, plan it, execute it in the background, persist state, and query progress at any time.

## Problem

Traditional automation requires explicit scripts and detailed instructions. ATLAS inverts that model: you describe *what* needs to happen, and the agent determines *how* to do it.

Example goal:

```json
{
  "goal": "Analyze the provided dataset and identify the major inconsistencies."
}
```

ATLAS will interpret the goal, generate a structured execution plan, run the plan asynchronously, record events, and reach a terminal state (`COMPLETED` or `FAILED`).

## Round 1 Architecture

```
┌─────────────┐     POST /missions      ┌──────────────────┐
│   Client    │ ──────────────────────► │   FastAPI API    │
└─────────────┘                         └────────┬─────────┘
       ▲                                         │
       │ GET /missions/{id}                      │ returns immediately (202)
       │                                         ▼
       │                                ┌──────────────────┐
       └────────────────────────────────│  Mission Service │
                                        └────────┬─────────┘
                                                 │
                    ┌────────────────────────────┼────────────────────────────┐
                    ▼                            ▼                            ▼
           ┌──────────────┐            ┌─────────────────┐           ┌─────────────────┐
           │  Repository  │            │ Background Exec │           │ Mission Planner │
           │   (SQLite)   │            │   (asyncio)     │           │ ADK / Local FB  │
           └──────────────┘            └────────┬────────┘           └─────────────────┘
                                                │
                                                ▼
                                       ┌─────────────────┐
                                       │ Workflow Runner │
                                       │ Plan → Execute  │
                                       └─────────────────┘
```

### Mission Lifecycle

```
CREATED → PLANNING → EXECUTING → COMPLETED
                              └→ FAILED
```

### Key Design Decisions

| Layer | Round 1 | Future |
|-------|---------|--------|
| **API** | FastAPI | Same |
| **Planner** | Gemini ADK or local fallback | Multi-agent delegation |
| **Execution** | Local asyncio background tasks | Cloud Pub/Sub |
| **Persistence** | SQLite | Firestore |
| **Deployment** | Local | Cloud Run |

## Technology Stack

- **Python 3.10+**
- **FastAPI** — HTTP API
- **Google ADK** — Agent framework (when configured)
- **Gemini** — Planning model (when configured)
- **aiosqlite** — Local persistence
- **pytest + httpx** — Automated tests

## Project Structure

```
atlas/
├── main.py                 # FastAPI application entry point
├── api/
│   ├── dependencies.py     # Dependency injection
│   └── routes/             # HTTP route handlers
├── config/
│   └── settings.py         # Environment-based configuration
├── domain/
│   ├── enums.py            # MissionStatus, EventType, etc.
│   └── models.py           # Pydantic domain models
├── agent/
│   ├── adk_planner.py      # Real Gemini/ADK planner
│   ├── local_planner.py    # Local development fallback
│   └── factory.py          # Planner selection
├── workflow/
│   ├── mission_runner.py   # Full mission lifecycle orchestration
│   └── step_executor.py    # Individual step execution
├── persistence/
│   ├── sqlite_repository.py
│   └── factory.py
├── execution/
│   ├── local_executor.py   # Background task dispatch
│   └── factory.py
└── services/
    └── mission_service.py  # Business logic

tests/                      # Automated test suite
```

## Environment Setup

### Prerequisites

- Python 3.10 or newer
- pip

### Installation

```bash
# Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
```

## Local Development

By default, ATLAS runs without any Google Cloud credentials using the **local development fallback planner**. This produces real structured plans and executes them — it just does not call Gemini.

```bash
# Start the server
uvicorn atlas.main:app --reload --host 0.0.0.0 --port 8000
```

### API Examples

**Health check:**

```bash
curl http://localhost:8000/health
```

**Create a mission:**

```bash
curl -X POST http://localhost:8000/missions \
  -H "Content-Type: application/json" \
  -d '{"goal": "Analyze the provided dataset and identify the major inconsistencies."}'
```

**Check mission status:**

```bash
curl http://localhost:8000/missions/{mission_id}
```

Interactive API docs are available at `http://localhost:8000/docs`.

## Running Tests

Tests use the local fallback planner and an isolated SQLite database. No paid cloud resources are required.

```bash
pytest -v
```

## Real Gemini / ADK Configuration

To use the real Gemini planner via Google ADK, set credentials in `.env`:

```env
PLANNER_BACKEND=auto
GOOGLE_API_KEY=your-api-key-here
GEMINI_MODEL=gemini-2.5-flash
```

For Vertex AI:

```env
PLANNER_BACKEND=adk
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GEMINI_MODEL=gemini-2.5-flash
```

When credentials are present, the health endpoint reports:

```json
{
  "planner_backend": "adk",
  "adk_configured": true
}
```

## Local Fallback Behavior

When no credentials are configured (or `PLANNER_BACKEND=local`), ATLAS uses `LocalFallbackPlanner`:

- Clearly labeled with `planner_source: "LOCAL_FALLBACK"`
- Produces deterministic, rule-based execution plans
- Never masquerades as Gemini output
- Fully runnable for development and CI

Force local fallback even with credentials present:

```env
PLANNER_BACKEND=local
```

## What Is Implemented (Round 1)

- [x] FastAPI backend with health and mission endpoints
- [x] Mission creation with immediate async return (HTTP 202)
- [x] Background workflow execution via asyncio
- [x] Structured execution plan generation
- [x] Step-by-step plan execution with state updates
- [x] Event recording throughout the lifecycle
- [x] SQLite persistence with repository abstraction
- [x] Gemini/ADK planner integration (when configured)
- [x] Clearly separated local development fallback
- [x] Automated test suite covering the full lifecycle

## Planned for Later Rounds

- Frontend UI
- Firestore persistence
- Cloud Pub/Sub for distributed execution
- Cloud Run deployment
- Multi-agent delegation
- File/dataset upload and analysis
- Memory bank and long-running workflows
- Authentication and production security
- Advanced failure recovery and retry

## License

Built for the Google All Things Agentic Hackathon 2026.
