# ATLAS

**Autonomous Task & Lifecycle Agent System**

ATLAS is an autonomous operations agent built for the Google All Things Agentic Hackathon 2026. It accepts a high-level goal, plans work, investigates uploaded CSV datasets with allowlisted tools, and produces an evidence-based report.

Round 6 adds a **real multi-agent operations layer**: a supervisor delegates bounded work to DATA_ANALYST, INVESTIGATOR, and REPORTER specialists, observes evidence, and replans. Round 5's Google Cloud path and Gemini/ADK integration remain.

## Problem

Traditional automation requires explicit scripts. A fixed investigation pipeline is only slightly better: it always runs the same analyses regardless of the goal.

ATLAS inverts that model for dataset missions:

1. Upload a CSV dataset.
2. Create a mission with a high-level investigation goal and `dataset_id`.
3. A supervisor decides which specialists should do which work, delegates tasks, inspects structured evidence, and may replan.
4. Facts come from allowlisted tools. Interpretation comes from the reporter. Those layers stay separate.

## What makes ATLAS agentic

ATLAS is agentic because a supervisor runs a bounded decision loop:

**MISSION → UNDERSTAND → DELEGATION PLAN → SPECIALISTS → TOOLS → EVIDENCE → OBSERVE → REPLAN / CONTINUE → SYNTHESIZE**

That loop:

- Interprets the mission goal.
- Matches work to specialists through an in-process agent registry.
- Delegates first-class tasks with dependencies, retries, and criticality.
- Invokes only allowlisted dataset tools from the specialist that owns them.
- Stores tool output as evidence.
- Changes subsequent work when evidence justifies it.
- Stops when the plan is done, a loop limit is reached, or a critical specialist cannot finish.
- Produces a final report that distinguishes observed facts from interpretation.

## Architecture

```
User
 ↓
Mission API
 ↓
Durable Mission Worker
 ↓
Supervisor
 ↓
Delegation Manager
 ↓
Specialized Agents (DATA_ANALYST / INVESTIGATOR / REPORTER)
 ↓
Allowlisted Tools
 ↓
Evidence
 ↓
Supervisor Replanning
 ↓
Final Synthesis
```

Cloud deployment still wraps this loop:

```
User → Cloud Run API → Firestore / Cloud Storage / Pub/Sub
                 → Cloud Run Worker → Supervisor → Specialists → Report
```

The same codebase runs locally by swapping backends through environment variables.

| Concern | LOCAL (default) | CLOUD |
|---------|-----------------|-------|
| Persistence | SQLite | Firestore |
| Dataset bytes | Local filesystem | Cloud Storage |
| Dispatch | In-process asyncio | Pub/Sub |
| HTTP process | `atlas.main:app` | Cloud Run API (`atlas.main:app`) |
| Worker | Same process as the API | Cloud Run worker (`atlas.worker:app`) |
| Planner / reasoner | Deterministic local fallback unless Gemini credentials exist | Gemini via Google ADK |

## Runtime modes

### LOCAL

Default developer experience. No Google Cloud credentials are required.

- SQLite at `ATLAS_DATABASE_PATH`
- CSV files under `ATLAS_UPLOAD_DIR`
- Local asyncio dispatcher (the API process runs the worker)
- If Gemini credentials are absent: `LOCAL_DEVELOPMENT_FALLBACK`
- If `GOOGLE_API_KEY` or Vertex AI is configured: `REAL_GEMINI_ADK` still works locally

### CLOUD

Configured with `ATLAS_RUNTIME_MODE=cloud` (or explicit backend overrides).

- Firestore for missions, events, plans, evidence, reports, and idempotency records
- Cloud Storage for uploaded datasets
- Pub/Sub for asynchronous dispatch
- Cloud Run API publishes `{mission_id, source}` messages
- Cloud Run worker receives the push, loads the durable mission from Firestore, claims a lease, and runs the supervisor / specialist loop

Local developers are not forced to provision these resources. Cloud-specific unit tests use in-memory fakes.

## Multi-agent operations

The durable worker does not run a hardcoded `analyst → investigator → reporter` pipeline. A **supervisor** owns the mission:

1. Understand the objective.
2. Match work to specialists via the in-process **Agent Registry**.
3. Create a persisted **delegation plan** of `SpecialistTask` objects (dependencies, criticality, retry limits).
4. Delegate ready tasks through a **Delegation Manager** (local in-process today; the contract can later publish to Pub/Sub without changing the domain).
5. Observe evidence.
6. Replan when results justify more work.
7. Ask the reporter to synthesize only after analysis work is complete enough.

| Specialist | Responsibility | Tools |
|------------|----------------|-------|
| DATA_ANALYST | Profile and measure quality issues | Full investigation allowlist |
| INVESTIGATOR | Connect findings and decide if more investigation is needed | `inspect_column` only |
| REPORTER | Prioritize findings and write the final report | None (interpretation only) |

Independent analyst measurements that only depend on the profile (for example missingness and duplicates) can run concurrently. A task does not start until its dependencies are `COMPLETED`.

Adaptive example: a duplicates-only goal does **not** start with outlier analysis. If the profile shows an extreme numeric range, the supervisor then delegates outlier analysis. A clean numeric file with the same goal does not get that extra work.

Delegation plans and specialist task states are stored on the mission document (SQLite JSON payload or Firestore). If a worker dies, recovery reclaims the mission lease and the supervisor resumes: `COMPLETED` specialist tasks are not rerun; `IN_PROGRESS` tasks return to `PENDING`.

`GET /missions/{id}` exposes `delegation_plan`, `current_objective`, specialist task results, and the existing `agent_plan` / evidence / report fields. It does not expose secrets or filesystem paths.

## Gemini / Google ADK

Hackathon requirement: **Gemini 3.5 or newer**.

- Model name is configured with `GEMINI_MODEL` (default `gemini-3.5-flash`).
- ATLAS never silently replaces a configured model with an older one.
- If the configured model is below 3.5, diagnostics set `gemini_meets_minimum=false` and a warning is logged. The exact name is still sent to the SDK.
- When credentials exist, planning, initial tool selection, and finding interpretation use Google ADK (`REAL_GEMINI_ADK`).
- Tool **execution** stays inside ATLAS. The model cannot run arbitrary tools, read files, or bypass the allowlist.
- When credentials are missing, the local fallback is used and labeled `LOCAL_DEVELOPMENT_FALLBACK`. It is never labeled as Gemini.

Transports:

- Gemini API: `GOOGLE_API_KEY`
- Vertex AI: `GOOGLE_GENAI_USE_VERTEXAI=true` and `GOOGLE_CLOUD_PROJECT`

`GET /health` reports `planner_label`, `gemini_model`, and `gemini_transport`. It never includes API keys.

## Mission lifecycle vs execution state

Lifecycle:

```
CREATED → PLANNING → EXECUTING → COMPLETED
                              └→ FAILED
```

Worker/dispatch state (durable, separate):

```
QUEUED → CLAIMED → RUNNING → COMPLETED
                           └→ FAILED
                           └→ EXHAUSTED (max attempts after lease expiry)
```

The API persists the mission as `QUEUED` and dispatches. It does not run the long-lived workflow inline. `GET /missions/{id}` includes `execution` metadata: state, attempt count, whether currently claimed, worker id while the lease is valid.

### Claiming and leases

A worker claims a mission atomically (SQLite `BEGIN IMMEDIATE`, or a Firestore transaction). Only one worker can hold a valid lease. Workflow updates must present the same `execution_id` and `worker_id`. Completed and failed missions cannot be claimed again.

### Recovery

`POST /ops/recover-missions` (local/dev convenience) finds incomplete missions whose leases have expired:

- If attempts remain: clear the lease, mark `QUEUED`, dispatch again.
- If `attempt_count >= max_attempts`: mark lifecycle `FAILED` and execution `EXHAUSTED`.
- Never touches `COMPLETED` or `FAILED` missions.

Recovery re-dispatches the workflow from the start of planning; it does not checkpoint mid-agent-loop.

### Idempotency

`POST /missions` accepts optional `idempotency_key` (max 128 characters):

- Same key + same `{goal, dataset_id}` → return the existing mission (HTTP 202).
- Same key + different payload → HTTP 409.
- No key → create a new mission.

### Pub/Sub worker security

A Pub/Sub message identifies a durable mission. It must not contain datasets, credentials, or secrets. The worker:

1. Parses `mission_id`.
2. Loads authoritative state from Firestore (or SQLite in local tests).
3. Ignores missing, terminal, or exhausted missions.
4. Claims the lease before executing.

Invalid push payloads return HTTP 400. Transient Firestore/Storage failures return HTTP 503 so Pub/Sub can retry with bounded backoff.

## Investigation tools

| Tool | What it measures |
|------|------------------|
| `profile_dataset` | Shape, column names, inferred types, numeric stats |
| `analyze_missing_values` | Per-column missing counts and percentages |
| `analyze_duplicates` | Exact duplicate rows |
| `analyze_type_format` | Numeric/datetime coercion failures; categorical variants |
| `analyze_outliers` | Tukey IQR (1.5×) on eligible numeric columns |
| `analyze_consistency` | Explicit start≤end and non-negative quantity-like rules |
| `inspect_column` | Follow-up on one named column already in the dataset |

Tools return structured evidence. They do not interpret impact. `inspect_column` is never part of the initial plan.

Adaptive follow-up is evidence-driven (extreme numeric range → outliers; highly missing columns → inspect; type anomalies → inspect). These branches do not always fire.

## Safety boundaries

Agent tools cannot:

- run shell commands
- evaluate arbitrary Python
- read filesystem paths or GCS bucket paths chosen by the model
- read environment secrets
- inspect columns that are not in the mission dataset
- accept arguments outside a per-tool allowlist

The dataset is bound in-memory as `ToolContext`. Object names in Cloud Storage are generated basenames under a configured prefix. Loop limits cap iterations, tool calls, and runtime.

## Technology stack

- Python 3.10+
- FastAPI
- pandas (deterministic CSV investigation)
- Google ADK + Gemini (when configured)
- aiosqlite (local)
- google-cloud-firestore, google-cloud-storage, google-cloud-pubsub (cloud)
- pytest + httpx

## Project structure

```
atlas/
├── api/routes/           # /health, /ready, /datasets, /missions, Pub/Sub push
├── agent/                # Tools, policy, planner, reasoner (ADK / local)
├── ops/                  # Supervisor, registry, delegation, specialists
├── investigation/        # Deterministic CSV analyzers used by tools
├── storage/              # Local filesystem or Cloud Storage
├── persistence/          # SQLite or Firestore
├── execution/            # Dispatcher, worker, Pub/Sub, recovery, leases
├── workflow/             # Mission lifecycle (generic + agent loop)
├── services/             # Mission and dataset business logic
├── domain/               # Models, enums, exceptions
├── config/               # Environment settings
├── main.py               # API entrypoint
└── worker.py             # Cloud Run worker entrypoint

docs/CLOUD.md             # Cloud Run provisioning and deploy commands
Dockerfile                # Single image for API and worker
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

Do not commit `.env` or service-account JSON files. Cloud Run should use its service identity. Local cloud experiments should use Application Default Credentials (`gcloud auth application-default login`).

## Local development

Default configuration uses SQLite, local files, and the local dispatcher. No Google credentials are required.

```bash
uvicorn atlas.main:app --reload --host 0.0.0.0 --port 8000
```

API docs: `http://localhost:8000/docs`

Useful variables (see `.env.example` for the full set):

```env
ATLAS_RUNTIME_MODE=local
PLANNER_BACKEND=auto
GEMINI_MODEL=gemini-3.5-flash
```

To use real Gemini locally without Cloud backends:

```env
ATLAS_RUNTIME_MODE=local
GOOGLE_API_KEY=your-api-key-here
GEMINI_MODEL=gemini-3.5-flash
PLANNER_BACKEND=auto
```

## API usage

**Health and readiness**

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/health` is healthy even when local fallback is active. `/ready` reports missing cloud configuration without calling Google APIs and without exposing secrets.

**Upload a CSV**

```bash
curl -X POST http://localhost:8000/datasets \
  -F "file=@path/to/data.csv;type=text/csv"
```

**Create a dataset investigation mission**

```bash
curl -X POST http://localhost:8000/missions \
  -H "Content-Type: application/json" \
  -d "{\"goal\": \"Analyze this survey dataset and identify the most important quality problems.\", \"dataset_id\": \"YOUR_DATASET_ID\"}"
```

Returns HTTP 202. Unknown `dataset_id` values return 404. Optional `idempotency_key` makes retries safe. If Pub/Sub publish fails in cloud mode, the API returns 503; the mission remains `QUEUED` and is not pretended to have been dispatched.

**Recover abandoned executions (local/dev)**

```bash
curl -X POST http://localhost:8000/ops/recover-missions
```

**Retrieve mission + plan + report**

```bash
curl http://localhost:8000/missions/{mission_id}
```

## Cloud setup and deployment

Implemented and documented. **Not claimed as already deployed** in a live project from this repository.

See [docs/CLOUD.md](docs/CLOUD.md) for:

- enabling APIs
- creating Firestore, a Cloud Storage bucket, and a Pub/Sub topic
- building the container
- deploying `atlas-api` and `atlas-worker`
- creating a push subscription
- verifying `/health` and a sample mission

Minimum cloud environment:

```env
ATLAS_RUNTIME_MODE=cloud
ATLAS_RUNTIME_ROLE=api
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1
ATLAS_GCS_BUCKET=your-atlas-datasets-bucket
ATLAS_PUBSUB_TOPIC=atlas-missions
GEMINI_MODEL=gemini-3.5-flash
GOOGLE_GENAI_USE_VERTEXAI=true
PLANNER_BACKEND=auto
```

Worker process:

```bash
python -m uvicorn atlas.worker:app --host 0.0.0.0 --port ${PORT:-8080}
```

The worker does not run a local asyncio background task. Cloud Run invokes `POST /internal/pubsub/push`.

## Running tests

The default suite uses the local fallback, a temp SQLite database, temp uploads, and in-memory fakes for Firestore/GCS/Pub/Sub. It does not require Google credentials and does not call Google APIs.

```bash
pytest -v
```

Opt-in live Google Cloud checks (only if you have credentials and intend to use a real project):

```bash
# Windows PowerShell
$env:ATLAS_LIVE_CLOUD="1"; pytest -v tests/integration
```

If that flag is unset, those tests are skipped.

## What is implemented

### Round 1

- FastAPI health and mission endpoints
- Async mission lifecycle with persisted state and events
- Planner abstraction (Gemini ADK or local fallback)

### Round 2

- CSV dataset upload with validation and safe storage names
- Missions that reference `dataset_id`
- Real CSV investigation analyzers
- Structured findings, deterministic prioritization, final report

### Round 3

- Structured, persisted agent plan
- Allowlisted investigation tools returning structured evidence
- Goal-based tool selection and adaptive follow-up
- Evidence records distinct from reasoner interpretations
- Configurable loop limits

### Round 4

- Mission submission separated from worker execution
- Durable execution metadata and exclusive leases
- Local dispatcher
- Bounded recovery of expired leases
- Optional idempotency keys

### Round 5

- Runtime mode configuration (`local` vs `cloud`)
- Real Gemini 3.5+ via Google ADK, with `GEMINI_MODEL` configuration
- Explicit `REAL_GEMINI_ADK` vs `LOCAL_DEVELOPMENT_FALLBACK` diagnostics
- Real Firestore mission/dataset repository (tested against an in-memory store)
- Real Cloud Storage dataset backend (tested with a fake client)
- Real Pub/Sub dispatcher and Cloud Run worker push entrypoint
- Dockerfile and Cloud Run deploy documentation
- Health/readiness reporting of active backends without secrets

### Round 6

- Supervisor / orchestrator owns the mission decision loop
- In-process Agent Registry with DATA_ANALYST, INVESTIGATOR, and REPORTER
- Persisted specialist tasks with dependencies, retries, and criticality
- Concurrent execution of independent analyst work
- Evidence-driven replanning (not a fixed three-agent pipeline)
- Mission recovery resumes from completed specialist tasks
- `GET /missions/{id}` exposes delegation plan and specialist task state

## Optional / future

These are **not** implemented in this round:

- A provisioned live GCP project, bucket, topic, or Cloud Run service from this repo
- Authentication / IAM beyond Cloud Run service identity
- Frontend UI
- Distributed agent registry, Memory Bank, Model Armor
- Multi-region deployment, GKE, or extra microservices
- Standing recovery scheduler (recovery remains an explicit call)
- Perfect mid-tool checkpointing (completed specialist tasks are skipped; in-flight tools restart)
- File types other than CSV

## Known limitations

- CSV only (no XLSX, PDF, or other formats)
- Consistency checks are a small explicit rule set
- Adaptive branches are a defined evidence-driven policy, not an unbounded model-authored workflow
- When Gemini is configured, it participates in initial capability selection and final interpretation; tool execution stays inside ATLAS
- Step execution for missions *without* a dataset is still a lifecycle demonstration, not specialist delegation
- Recovery is on-demand. Completed specialist tasks are not rerun; a tool that died mid-call is retried
- Delegation is in-process in the mission worker (local asyncio or the Cloud Run worker). There is no separate specialist fleet
- Default tests always force local fallback and do not call paid APIs
- Live Google Cloud verification depends on credentials in the environment running the tests

## License

Built for the Google All Things Agentic Hackathon 2026.
