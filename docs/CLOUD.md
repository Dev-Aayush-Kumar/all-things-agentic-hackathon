# ATLAS Cloud Run deployment

This document describes how to deploy the **implemented** Cloud Run path. It does not claim that a live Google Cloud project has already been provisioned.

ATLAS uses one container image and two services:

| Service | Entrypoint | Role |
|---------|------------|------|
| `atlas-api` | `atlas.main:app` | HTTP API: health, datasets, missions |
| `atlas-worker` | `atlas.worker:app` | Pub/Sub push consumer that executes missions |

## Prerequisites

1. A Google Cloud project with billing enabled.
2. APIs enabled: Cloud Run, Artifact Registry, Firestore, Cloud Storage, Pub/Sub, Vertex AI (if using Vertex).
3. Application Default Credentials locally (`gcloud auth application-default login`), or a Cloud Run service account at deploy time.
4. Do **not** put service-account JSON files in the repository.

## Provision resources

```bash
PROJECT_ID=your-gcp-project-id
REGION=us-central1
BUCKET=your-atlas-datasets-bucket

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com storage.googleapis.com pubsub.googleapis.com aiplatform.googleapis.com

gcloud firestore databases create --location="$REGION" --type=firestore-native || true
gcloud storage buckets create "gs://$BUCKET" --location="$REGION"
gcloud pubsub topics create atlas-missions
```

## Build the image

```bash
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/atlas:latest"
```

Or locally:

```bash
docker build -t atlas:latest .
docker run --rm -p 8080:8080 \
  -e ATLAS_RUNTIME_MODE=local \
  -e PLANNER_BACKEND=local \
  atlas:latest
```

The local `docker run` example above is only a container smoke test. It still uses SQLite unless you set cloud environment variables and credentials.

## Deploy the API

```bash
gcloud run deploy atlas-api \
  --image "gcr.io/${PROJECT_ID}/atlas:latest" \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "ATLAS_RUNTIME_MODE=cloud,ATLAS_RUNTIME_ROLE=api,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},ATLAS_GCS_BUCKET=${BUCKET},ATLAS_PUBSUB_TOPIC=atlas-missions,GEMINI_MODEL=gemini-3.5-flash,GOOGLE_GENAI_USE_VERTEXAI=true,PLANNER_BACKEND=auto"
```

`--allow-unauthenticated` is a hackathon convenience. Restrict ingress before any real production use.

## Deploy the worker

```bash
gcloud run deploy atlas-worker \
  --image "gcr.io/${PROJECT_ID}/atlas:latest" \
  --region "$REGION" \
  --no-allow-unauthenticated \
  --command python \
  --args "-m,uvicorn,atlas.worker:app,--host,0.0.0.0,--port,8080" \
  --set-env-vars "ATLAS_RUNTIME_MODE=cloud,ATLAS_RUNTIME_ROLE=worker,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},ATLAS_GCS_BUCKET=${BUCKET},ATLAS_PUBSUB_TOPIC=atlas-missions,GEMINI_MODEL=gemini-3.5-flash,GOOGLE_GENAI_USE_VERTEXAI=true,PLANNER_BACKEND=auto"
```

Create a push subscription that targets the worker:

```bash
WORKER_URL="$(gcloud run services describe atlas-worker --region "$REGION" --format='value(status.url)')"

gcloud pubsub subscriptions create atlas-missions-worker \
  --topic atlas-missions \
  --push-endpoint "${WORKER_URL}/internal/pubsub/push" \
  --push-auth-service-account "atlas-pubsub@${PROJECT_ID}.iam.gserviceaccount.com"
```

Grant Pub/Sub permission to invoke the worker. The worker loads the mission from Firestore using the `mission_id` in the message. It does not trust datasets or secrets from the payload.

## Verify

```bash
API_URL="$(gcloud run services describe atlas-api --region "$REGION" --format='value(status.url)')"
curl "${API_URL}/health"
curl "${API_URL}/ready"
```

`/health` reports the active backends and `planner_label` (`REAL_GEMINI_ADK` or `LOCAL_DEVELOPMENT_FALLBACK`). It never includes API keys.

Then upload a CSV and create a mission as in the README. Confirm:

1. The API returns HTTP 202.
2. A Pub/Sub message is published.
3. The worker executes the mission.
4. `GET /missions/{id}` reaches `COMPLETED` or an explicit `FAILED` state.

## Local tests vs live tests

```bash
pytest -v
```

The default suite uses SQLite, local files, and fakes. It does not call Google Cloud.

Opt-in live checks:

```bash
ATLAS_LIVE_CLOUD=1 pytest -v tests/integration
```

Those tests run only when you explicitly enable them and credentials are present.
