# ATLAS

**Autonomous Task & Lifecycle Agent System**

ATLAS is an autonomous operations agent built for the Google All Things Agentic Hackathon 2026. Instead of requiring step-by-step instructions, ATLAS accepts a high-level goal and autonomously plans, executes, and reports on the work.

> **Round 2 focus:** Perform a real autonomous CSV data investigation. Deterministic Python measures facts from the uploaded file. The planner/reasoner interprets those facts against the user's goal. Findings are never canned or simulated.

## Problem

Traditional automation requires explicit scripts. ATLAS inverts that model: you describe *what* needs to happen, and the agent determines *how* to do it.

Round 2 target:

1. Upload a CSV dataset.
2. Create a mission with a high-level investigation goal and `dataset_id`.
3. ATLAS inspects the real file, measures quality issues, prioritizes them, and returns a structured report.

## Architecture

```
┌─────────────┐   POST /datasets     ┌──────────────────┐
│   Client    │ ───────────────────► │   FastAPI API    │
│             │   POST /missions     └────────┬─────────┘
└─────────────┘                               │ 202 immediately
       ▲                                      ▼
       │ GET /missions/{id}          ┌──────────────────┐
       └─────────────────────────────│  Mission Service │
                                     └────────┬─────────┘
          ┌──────────────┬────────────────────┼─────────────────┐
          ▼              ▼                    ▼                 ▼
   Dataset store   SQLite metadata    Background exec     Planner / reasoner
   (local files)   (missions+datasets)   (asyncio)        (ADK or local)
                                              │
                                              ▼
                                   Workflow: plan → execute
                                   Dataset missions run the
                                   investigation pipeline
```

### Mission lifecycle

```
CREATED → PLANNING → EXECUTING → COMPLETED
                              └→ FAILED
```

Dataset missions add investigation stages during `EXECUTING`. Missions without a `dataset_id` keep the Round 1 generic lifecycle.

### Facts vs reasoning

| Layer | Responsibility |
|-------|----------------|
| **Deterministic pipeline** | Profile, missing values, duplicates, type/format anomalies, IQR outliers, explicit consistency rules |
| **Planner** | Turn the goal into a structured execution plan |
| **Reasoner** | Summarize measured findings, explain impact, organize a resolution plan |

The reasoner is not allowed to invent findings, metrics, or columns.

### Replaceable infrastructure

| Layer | Round 2 | Later |
|-------|---------|--------|
| Dataset bytes | Local filesystem (`data/uploads`) | Cloud Storage |
| Metadata | SQLite | Firestore |
| Background work | asyncio tasks | Pub/Sub |
| Planner / reasoner | Gemini ADK or local fallback | Multi-agent fleet |

## Technology stack

- Python 3.10+
- FastAPI
- pandas (deterministic CSV investigation)
- Google ADK + Gemini (when configured)
- aiosqlite
- pytest + httpx

## Project structure

```
atlas/
├── api/routes/           # /health, /datasets, /missions
├── agent/                # Planner + investigation reasoner (ADK / local)
├── investigation/        # Deterministic CSV analysis pipeline
├── storage/              # Dataset byte storage (local; Cloud Storage later)
├── persistence/          # SQLite mission + dataset metadata
├── workflow/             # Mission lifecycle (generic + dataset investigation)
├── services/             # Mission and dataset business logic
├── domain/               # Models, enums, exceptions
└── config/               # Environment settings

tests/
├── fixtures/             # CSV fixtures with intentional quality issues
└── test_*.py
```

## Environment setup

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

## Local development

Default configuration uses the **local development fallback** planner/reasoner. No Google credentials are required. Dataset investigation still runs against the real uploaded CSV.

```bash
uvicorn atlas.main:app --reload --host 0.0.0.0 --port 8000
```

API docs: `http://localhost:8000/docs`

## API usage

**Health**

```bash
curl http://localhost:8000/health
```

**Upload a CSV**

```bash
curl -X POST http://localhost:8000/datasets \
  -F "file=@path/to/data.csv;type=text/csv"
```

Returns `dataset_id`, original filename, generated stored filename, content type, size, and `created_at`. Filesystem paths are not exposed. Only CSV is accepted. Empty files, unsupported types, and oversized uploads are rejected. Stored names are generated UUIDs; original names are sanitized.

**Create a dataset investigation mission**

```bash
curl -X POST http://localhost:8000/missions \
  -H "Content-Type: application/json" \
  -d "{\"goal\": \"Analyze this dataset, identify important data quality problems and inconsistencies, investigate likely causes, prioritize the issues, and produce a concrete resolution plan.\", \"dataset_id\": \"YOUR_DATASET_ID\"}"
```

Returns immediately with HTTP 202. Unknown `dataset_id` values return 404.

**Create a Round 1 mission (no dataset)**

```bash
curl -X POST http://localhost:8000/missions \
  -H "Content-Type: application/json" \
  -d "{\"goal\": \"Review system logs and summarize anomalies.\"}"
```

**Retrieve mission + report**

```bash
curl http://localhost:8000/missions/{mission_id}
```

When a dataset mission completes, the payload includes `investigation_report` with dataset summary, prioritized findings (evidence + metrics), recommended actions, and overall assessment.

**Retrieve dataset metadata**

```bash
curl http://localhost:8000/datasets/{dataset_id}
```

## Investigation capabilities (Round 2)

Supported input: **CSV only**.

Stages (each recorded as a mission event):

1. **Dataset profile** — row/column counts, names, inferred types, numeric min/max/mean/median/std
2. **Missing data** — per-column null counts and percentages; columns ≥20% missing are marked materially incomplete
3. **Duplicates** — exact duplicate row count and percentage
4. **Type / format anomalies** — values that fail numeric or datetime coercion; categorical casing/whitespace variants
5. **Outliers** — Tukey IQR (1.5×) on numeric columns with enough values; skipped for binary or identifier-like columns
6. **Cross-column consistency** — explicit rules only:
   - start/begin vs end/finish datetime columns: start must be ≤ end
   - columns named quantity/qty/count/age/price/amount/duration: values should not be negative
7. **Prioritization** — deterministic rank (1 = highest) from affected-record percentage, category weight, and severity
8. **Report** — structured JSON from measured findings plus reasoner interpretation

ATLAS does **not** infer arbitrary semantic relationships between columns. If a consistency rule cannot be justified, it is not reported.

## Gemini / ADK vs local fallback

Set credentials in `.env` to use real Gemini via Google ADK for planning and finding interpretation:

```env
PLANNER_BACKEND=auto
GOOGLE_API_KEY=your-api-key-here
GEMINI_MODEL=gemini-2.5-flash
```

Without credentials, or with `PLANNER_BACKEND=local`:

- `planner_source` / `reasoning_source` is `LOCAL_FALLBACK`
- Plans and summaries are rule/template based
- They are never labeled as Gemini output
- CSV measurements still come from the real file

## Running tests

Tests use the local fallback, a temp SQLite database, and a temp upload directory. No paid cloud resources are required.

```bash
pytest -v
```

## What is implemented

### Round 1

- FastAPI health and mission endpoints
- Async mission lifecycle with persisted state and events
- Planner abstraction (Gemini ADK or local fallback)

### Round 2

- CSV dataset upload with validation and safe storage names
- Missions that reference `dataset_id`
- Real CSV investigation pipeline
- Structured findings, deterministic prioritization, final report
- Investigation-stage events
- Parse/analysis failures → `FAILED` with error details
- Round 1 missions without a dataset still work

## Known limitations

- CSV only (no XLSX, PDF, or other formats)
- Consistency checks are a small explicit rule set, not general semantic understanding
- Step execution for missions *without* a dataset is still a lifecycle demonstration, not domain work
- Local filesystem + SQLite (not Cloud Storage / Firestore yet)
- Local asyncio background tasks (not Pub/Sub)
- No frontend, authentication, or production deployment
- Gemini/ADK interpretation is used only when credentials are configured; this repo's tests always use the local fallback

## Planned for later rounds

- Frontend UI
- Firestore and Cloud Storage
- Pub/Sub and Cloud Run
- Multi-agent delegation
- Additional file types
- Authentication and production security

## License

Built for the Google All Things Agentic Hackathon 2026.
