# ATLAS

**Autonomous Task & Lifecycle Agent System**

ATLAS is an autonomous operations agent built for the Google All Things Agentic Hackathon 2026. It accepts a high-level goal, plans work, investigates uploaded CSV datasets with allowlisted tools, can apply **controlled remediations** to a working copy, and produces an evidence-based report.

Round 10 adds **persistent memory**: after a mission completes, ATLAS extracts structured knowledge, stores it, and can retrieve a bounded set into later reasoning. Memory is advisory. Current evidence wins. Rounds 5–9 remain.

## Problem

Traditional automation requires explicit scripts. A fixed investigation pipeline is only slightly better: it always runs the same analyses regardless of the goal.

ATLAS inverts that model for dataset missions:

1. Upload a CSV dataset.
2. Create a mission with a high-level investigation goal and `dataset_id`.
3. A supervisor asks a typed decision-maker what to do next. Gemini/ADK when configured; deterministic local fallback otherwise.
4. ATLAS validates every decision against the capability catalog, Agent Registry, Action Registry, and External Tool Registry.
5. If a remediation is authorized, it runs only on a working copy — never the uploaded original — and must pass postcondition verification.
6. If an external capability is authorized, ATLAS executes the registered tool and stores a bounded evidence excerpt with provenance.
7. Facts come from allowlisted observation tools. External pages are labeled separately and never silently override dataset measurements. Decisions come from the model or local policy. Execution stays inside ATLAS.

## What makes ATLAS agentic

ATLAS is agentic because a supervisor runs a bounded decision loop:

**MISSION → MODEL DECISION → ATLAS VALIDATES → ATLAS EXECUTES → ATLAS OBSERVES → MODEL SEES EVIDENCE → MODEL DECIDES AGAIN → REPORT**

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
MISSION GOAL + STRUCTURED CONTEXT
        ↓
Gemini / local decision-maker
        ↓  typed DELEGATE | OBSERVE | ACTION | EXTERNAL | COMPLETE
ATLAS validates (catalog + registries + limits)
        ↓
ATLAS executes specialists / tools / actions / registered external tools
        ↓
Structured evidence (never raw CSV or raw HTML to the model)
        ↓
Supervisor feeds evidence back
        ↓
Next typed decision or COMPLETE
```

Gemini is the decision-maker when configured. ATLAS is the enforcement and execution environment. Gemini never executes HTTP, shell, or Python. The model cannot bypass registries, verification, or loop limits.

Specialists remain role-scoped:

```
Supervisor
 ├─ DATA_ANALYST   observation tools only
 ├─ INVESTIGATOR   evidence interpretation / inspect_column
 ├─ REMEDIATOR     allowlisted dataset actions only
 └─ REPORTER       synthesis only
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

| Specialist | Responsibility | Tools / actions |
|------------|----------------|-----------------|
| DATA_ANALYST | Profile and measure quality issues | Full investigation allowlist (observation) |
| INVESTIGATOR | Connect findings and decide if more investigation is needed | `inspect_column` only |
| REMEDIATOR | Execute allowlisted remediations on a working copy | No observation tools. Actions only via the Action Registry |
| REPORTER | Prioritize findings and write the final report | None (interpretation only) |

Independent analyst measurements that only depend on the profile (for example missingness and duplicates) can run concurrently. A task does not start until its dependencies are `COMPLETED`.

Adaptive example: a duplicates-only goal does **not** start with outlier analysis. If the profile shows an extreme numeric range, the supervisor then delegates outlier analysis. A clean numeric file with the same goal does not get that extra work.

Delegation plans and specialist task states are stored on the mission document (SQLite JSON payload or Firestore). If a worker dies, recovery reclaims the mission lease and the supervisor resumes: `COMPLETED` specialist tasks are not rerun; `IN_PROGRESS` tasks return to `PENDING`.

`GET /missions/{id}` exposes `delegation_plan`, `current_objective`, specialist task results, `actions`, `working_copy`, `reasoning_trace`, `external_invocations`, and the existing `agent_plan` / evidence / report fields. It does not expose secrets, raw internals, or filesystem paths.

## External tools

Gemini (or local fallback) may propose a registered external capability. ATLAS remains the only component that talks to the network.

```
Model proposes EXTERNAL FETCH_URL
        ↓
Typed decision validation
        ↓
External Tool Registry (known?)
        ↓
Policy (enabled? authorized for this mission? budget?)
        ↓
URL / network guard (scheme, allowlist, private/loopback/link-local)
        ↓
ATLAS executes FETCH_URL
        ↓
Bounded structured evidence + provenance
        ↓
Next reasoning iteration sees an excerpt, not HTML
```

Registered in this round:

| Capability | Model may supply | ATLAS controls |
|------------|------------------|----------------|
| `FETCH_URL` | `url` only | headers, cookies, auth, timeout, redirects, size limit, User-Agent |

Configuration (fail closed):

- `ATLAS_EXTERNAL_TOOLS_ENABLED` / `ATLAS_FETCH_URL_ENABLED`
- `ATLAS_FETCH_ALLOWED_DOMAINS` — empty means **no host is authorized**
- `ATLAS_FETCH_ALLOWED_SCHEMES` (default `https,http`)
- `ATLAS_FETCH_TIMEOUT_SECONDS`, `ATLAS_FETCH_MAX_BYTES`, `ATLAS_FETCH_MAX_REDIRECTS`
- `ATLAS_FETCH_ALLOW_LOOPBACK` — default `false`. Loopback is allowed only when this is true **and** the host is on the domain allowlist (intended for tests)

Local fallback proposes `FETCH_URL` only when the mission goal contains a URL whose host is already on the allowlist, and only after a dataset profile exists. It does not fetch arbitrary URLs.

Dataset-only missions never need this layer.

### SSRF limitations (honest)

ATLAS rejects localhost, loopback, link-local, private, CGNAT, and unspecified addresses after DNS resolution, and re-validates every redirect hop. This is not a perfect SSRF shield:

- There is a TOCTOU gap between DNS resolution and the TCP/TLS connect (DNS rebinding).
- ATLAS does not pin the connection to the pre-resolved IP (doing so would break ordinary TLS SNI).
- Rare hostname encodings that the resolver treats as public may still be attempted; unknown schemes are rejected.
- `ATLAS_FETCH_ALLOW_LOOPBACK` is an explicit test/dev escape hatch and must stay off in production.

## Gemini / Google ADK

Hackathon requirement: **Gemini 3.5 or newer**.

- Model name is configured with `GEMINI_MODEL` (default `gemini-3.5-flash`).
- ATLAS never silently replaces a configured model with an older one.
- If the configured model is below 3.5, diagnostics set `gemini_meets_minimum=false` and a warning is logged. The exact name is still sent to the SDK.
- When credentials exist, planning, tool selection, supervisor decisions, and finding interpretation use Google ADK (`REAL_GEMINI_ADK` / `GEMINI_ADK`).
- The supervisor asks Gemini for a **typed** decision (`DELEGATE`, `OBSERVE`, `ACTION`, `EXTERNAL`, `COMPLETE`). Malformed output is rejected, not guessed.
- Tool, action, and external **execution** stay inside ATLAS. The model cannot run arbitrary tools, fetch arbitrary URLs, read files, write the source dataset, or bypass the allowlists.
- Gemini proposing an action is not authorization. ATLAS still runs ActionRegistry, parameter checks, execution, and postcondition verification.
- Structured evidence (counts, findings, verification) is fed back into the next reasoning iteration. The raw CSV is not sent to Gemini.
- When Gemini is unavailable, `LocalDecisionMaker` implements the same typed contract from deterministic policy and is labeled `LOCAL_FALLBACK`. It is never labeled as Gemini.
- A live Gemini failure on one iteration falls back to the local decider for that step and records the failure.

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

## Observation vs action

**Observation** tools gather evidence. They do not change the dataset.

**Action** tools change a controlled working copy. They are a separate allowlist.

| | Observation | Action |
|--|-------------|--------|
| Purpose | Measure | Change |
| Who may run it | DATA_ANALYST / INVESTIGATOR (scoped tools) | REMEDIATOR only |
| Target | In-memory bound dataset / current working frame | Working copy only |
| Success | Structured evidence recorded | Postcondition **verified** |

Gemini never sits on the action execution path. The model may propose or interpret. ATLAS validates authorization and parameters, executes, and verifies.

### Action registry and authorization

Every action declares identity, description, input schema, output schema, risk, and allowed agents. Unknown actions, unauthorized agents, and malformed parameters are rejected before any bytes are written.

Registered remediations in this round:

| Action | Effect | Verification |
|--------|--------|--------------|
| `REMOVE_DUPLICATES` | `drop_duplicates(keep="first")` on the working copy | Duplicate count is 0 and row count equals the prior unique count |
| `FILL_MISSING_VALUES` | Fill one named column (`auto`: numeric median or `UNKNOWN`) | That column has 0 missing values and the row count is unchanged |

ATLAS does **not** run every remediation. The supervisor proposes an action only when:

1. The mission goal explicitly asks to fix / remediate / clean / repair (not “what should be fixed first”).
2. Current evidence justifies that specific action (duplicate rows, or material missingness ≥ 20%).
3. Mission action limits have not been reached (`ATLAS_MAX_ACTIONS`, default 4).

### Working copies

```
SOURCE DATASET
    ↓
IMMUTABLE ORIGINAL
    ↓
WORKING COPY v1, v2, …   ← controlled transformations
```

The uploaded source file is never overwritten. Each verified action writes a new generated basename (`wcopy_{mission_id}_vN.csv`) and records version metadata, parent version, and the action that created it. Later actions read the latest verified version.

### Verification, failure, and idempotency

Success is not “the function returned.” Success is a recorded before/after state whose postcondition passed.

If verification fails:

- the original source stays untouched
- the working-copy version is not advanced
- the action is `VERIFICATION_FAILED`
- the specialist task may retry up to `ATLAS_ACTION_MAX_ATTEMPTS` (default 2)
- the supervisor may continue without treating the action as successful

Idempotency key: `sha256(mission_id + action_type + canonical parameters + input_version)`. A verified key is reused and does not write another version. A worker crash during `RUNNING` resets the action to `PROPOSED` and retries from the last **verified** working copy.

Recovery of the durable mission lease is unchanged. Completed remediator tasks are not rerun. Interrupted actions retry safely; they do not claim transactional filesystem semantics.

### Supervisor action loop

```
UNDERSTAND → PLAN → DELEGATE → OBSERVE → DECIDE
 → ACT → VERIFY → OBSERVE → REPLAN → ACT AGAIN OR COMPLETE → REPORT
```

Existing mission / agent iteration, tool-call, and runtime limits still apply. Actions have their own cap. The loop stops when the objective is satisfied, a limit is hit, or a critical specialist cannot finish.

Local execution of actions is fully functional (SQLite + local files). Cloud workers reuse the same `ActionExecutor` and persist working-copy bytes through the existing Storage interface (GCS) and mission document (Firestore). There is no second broker.

## Safety boundaries

Agent tools and actions cannot:

- run shell commands
- evaluate arbitrary Python (`eval` / `exec`)
- make arbitrary HTTP requests
- read filesystem paths or GCS bucket paths chosen by the model
- overwrite the uploaded source dataset
- write unrestricted files or databases
- read environment secrets
- inspect columns that are not in the mission dataset
- accept arguments outside a per-tool or per-action allowlist
- send email, make payments, or touch production systems

The dataset is bound in-memory as `ToolContext`. Actions write only generated working-copy basenames through `DatasetStorage`. Object names in Cloud Storage stay under a configured prefix. Loop limits cap iterations, tool calls, runtime, and actions.

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
├── ops/                  # Supervisor, registry, delegation, specialists, actions
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
ATLAS_MAX_ACTIONS=4
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

### Round 7

- First-class `ActionRecord` / verification / working-copy models
- Action Registry with authorization and parameter schemas
- REMEDIATOR specialist; observation vs action are separate allowlists
- `REMOVE_DUPLICATES` and `FILL_MISSING_VALUES` operate only on working copies
- Original uploads remain immutable
- Mandatory verification; failed postconditions do not advance the working copy
- Bounded retry and idempotent re-execution after worker interruption
- Supervisor proposes actions from evidence + goal, then re-observes
- `GET /missions/{id}` exposes actions, verification, and working-copy version
- Action lifecycle events (`ACTION_PROPOSED` … `REPLAN_AFTER_ACTION`)

### Round 8

- Typed supervisor decisions (`DELEGATE`, `OBSERVE`, `ACTION`, `COMPLETE`)
- Machine-readable capability catalog exposed to the decision-maker
- Gemini/ADK drives the supervisor loop when configured; ATLAS still validates and executes
- Local fallback implements the same decision contract and is labeled `LOCAL_FALLBACK`
- Structured evidence is fed back into the next reasoning iteration
- Repeated identical decisions and model-call counts are bounded
- `GET /missions/{id}` exposes `reasoning_trace`
- Events: `MODEL_REASONING_STARTED`, `MODEL_DECISION_RECEIVED`, `MODEL_DECISION_VALIDATED`, `MODEL_DECISION_REJECTED`, `SPECIALIST_DELEGATED`, `OBSERVATION_REQUESTED`, `MODEL_REPLANNED`, `MODEL_COMPLETED`

### Round 9

- External Tool Registry with a typed `EXTERNAL` decision
- `FETCH_URL` registered retrieval tool; model supplies only `url`
- Domain allowlist, scheme policy, timeout, size, and redirect re-validation
- Private/loopback/link-local destinations rejected by default
- External results become bounded evidence with provenance (`source_type=EXTERNAL`)
- Failures are recorded as failed invocations, never as successful evidence
- Report `external_references` is separate from dataset findings
- Local fallback proposes `FETCH_URL` only for an allowlisted URL found in the goal
- Events: `EXTERNAL_TOOL_PROPOSED` … `EXTERNAL_TOOL_REJECTED`

### Round 10

- Explicit `MemoryRecord` (FACT / PROCEDURE / INSIGHT / PREFERENCE) with provenance, scope, confidence, and fingerprint
- Post-completion extraction: local deterministic extractor, or Gemini proposals that ATLAS validates before persist
- SQLite and Firestore repositories; retrieval is bounded lexical/tag overlap (no vector DB)
- `relevant_memory` is a separate reasoning-context section and is never current evidence
- Local fallback can select extra allowlisted observations because of retrieved PROCEDURE/INSIGHT memories
- `GET /memory` and `GET /memory/{id}` for inspection
- Events: `MEMORY_EXTRACTION_STARTED`, `MEMORY_EXTRACTED`, `MEMORY_MERGED`, `MEMORY_REJECTED`, `MEMORY_EXTRACTION_FAILED`

## Memory

ATLAS memory is **not** a transcript dump and **not** a RAG platform.

```
Mission A completes
        ↓
Extractor proposes typed memories
        ↓
ATLAS validates (type, provenance, size, secrets, metadata)
        ↓
Fingerprint dedupe / merge
        ↓
SQLite or Firestore
        ↓
Mission B retrieves a bounded set
        ↓
relevant_memory in reasoning context
        ↓
Decision maker may propose extra allowlisted work
        ↓
Existing validation still applies
```

| Kind | Meaning |
|------|---------|
| FACT | Dataset- or mission-scoped observation (not a universal rule) |
| PROCEDURE | Reusable investigation habit |
| INSIGHT | Reusable interpretation, still advisory |
| PREFERENCE | Supported but not auto-extracted in this round |

**Confidence policy:** deterministic evidence 0.80; local insight/procedure 0.70; Gemini proposals capped at 0.55. A new supporting mission adds 0.05, max 0.95. This is an explicit policy, not model-reported truth.

**Scope:** FACT is never global. GLOBAL is only for PROCEDURE/INSIGHT. A finding about one CSV does not become “all age columns everywhere.”

**Retrieval:** token/tag overlap plus scope filters. Limit `ATLAS_MEMORY_MAX_RETRIEVAL`. A future embedding backend can replace `MemoryRetriever` without changing the supervisor.

**Safety:** memory text is never a tool definition. `EXECUTE_SHELL` in a memory cannot execute. Invalid arguments (missing columns, unknown tools) still fail validation. Current measurements override conflicting history.

Gemini may propose memories when configured. It cannot write SQLite/Firestore or skip validation. Local extraction is labeled `LOCAL_FALLBACK` / `DETERMINISTIC_EVIDENCE`.

Vector search is intentionally omitted: the vertical slice is durable, inspectable, and cheap.

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
- When Gemini is configured it proposes typed supervisor decisions; ATLAS still executes tools and actions
- Default tests use scripted/local decisions and do not call live Gemini
- Live Gemini supervisor decisions depend on credentials in the environment running the process
- Working-copy writes are not a transactional filesystem: a crash after a successful write and before persist can leave an unused object; retry uses the last verified version
- Only dataset remediations inside the working-copy sandbox; no arbitrary external side effects
- External fetches are limited to registered tools and the configured domain allowlist
- DNS-rebinding TOCTOU remains (see External tools)
- Step execution for missions *without* a dataset is still a lifecycle demonstration, not specialist delegation
- Recovery is on-demand. Completed specialist tasks are not rerun; a tool that died mid-call is retried
- Delegation is in-process in the mission worker (local asyncio or the Cloud Run worker). There is no separate specialist fleet
- Default tests always force local fallback and do not call paid APIs
- Live Google Cloud verification depends on credentials in the environment running the tests
- Memory retrieval is lexical, not semantic; similar phrasing may miss a relevant memory
- Memory extraction is post-completion enrichment and does not fail the mission if it errors

## License

Built for the Google All Things Agentic Hackathon 2026.
