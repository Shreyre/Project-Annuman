#!/usr/bin/env bash
# Deploy this branch (demo-day) as its own Cloud Run service, anumaan-next, next to the judged one.
#   bash deploy/deploy_next.sh
# It never touches the judged service "anumaan": another service name has its own URL, revisions,
# instance and in-memory state. What differs from deploy/deploy.sh:
# - min-instances 0: idle costs nothing, and the first request after a quiet spell waits for a cold
#   start. max-instances 1, as there: shelf checks, approvals, what-if worlds and the live feed are held
#   in the instance's memory, so a second instance would answer from a different state.
# - No Pub/Sub topic and no ingest token: --set-env-vars replaces the whole environment, so the live
#   feed is applied in-process and /api/ingest refuses every message (403). Do not run deploy/live.sh
#   for it: live.sh works on the judged service and would give it a new revision.
# - Private: only accounts with roles/run.invoker can open it (the commands below open it up; a re-run
#   of this script makes it private again). Its Gemini calls share the project's Vertex quota with
#   the judged service.
# The same runtime identity as deploy.sh (anumaan-run: roles/aiplatform.user only), which deploy.sh
# created. Every call passes --project; this script never changes your gcloud config.
set -euo pipefail

PROJECT=anumaan-c4c
REGION=asia-south1
SERVICE=anumaan-next
MODEL=gemini-3.7-flash
SA="anumaan-run@$PROJECT.iam.gserviceaccount.com"

[ "$SERVICE" != anumaan ] || { echo "refusing to deploy over the judged service" >&2; exit 1; }
cd "$(dirname "$0")/.."

# builds with Cloud Build from the Dockerfile, as deploy.sh does. The image holds anumaan/, app/,
# grammar/ and the paper intake's sample pages (tools/samples/, served at /samples).
gcloud run deploy "$SERVICE" --source . --project "$PROJECT" --region "$REGION" \
  --service-account "$SA" --min-instances 0 --max-instances 1 --memory 1Gi \
  --no-allow-unauthenticated \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=global,ANUMAAN_MODEL=$MODEL"

gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)'

# One-time commands you may need; this script runs none of them.
#
# APIs: deploy.sh already enabled run, cloudbuild, artifactregistry and aiplatform on anumaan-c4c. The
# brief's read-aloud uses Gemini-TTS through Vertex AI (aiplatform), so texttospeech.googleapis.com is
# NOT needed. On a fresh project:
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com aiplatform.googleapis.com --project anumaan-c4c
#
# Open it while it is private (then browse http://localhost:8080):
#   gcloud run services proxy anumaan-next --project anumaan-c4c --region asia-south1
# Make it public for Demo Day, and private again afterwards:
#   gcloud run services add-iam-policy-binding anumaan-next --project anumaan-c4c --region asia-south1 --member allUsers --role roles/run.invoker
#   gcloud run services remove-iam-policy-binding anumaan-next --project anumaan-c4c --region asia-south1 --member allUsers --role roles/run.invoker
#
# No cold start on the day: keep one instance warm and build the three networks and the three menu
# what-ifs before it takes traffic (10.5 s and 502 MB on a laptop; not measured on Cloud Run). An idle
# warm instance is billed, so undo it afterwards:
#   gcloud run services update anumaan-next --project anumaan-c4c --region asia-south1 --min-instances 1 --update-env-vars ANUMAAN_WARM=1
#   gcloud run services update anumaan-next --project anumaan-c4c --region asia-south1 --min-instances 0 --remove-env-vars ANUMAAN_WARM
#
# Google retires the gemini-2.5-flash text model on 20 Oct 2026 and lists no date for gemini-2.5-flash-tts.
# If read-aloud starts answering 404, switch the TTS model (untested; its voices are all Preview):
#   gcloud run services update anumaan-next --project anumaan-c4c --region asia-south1 --update-env-vars ANUMAAN_TTS_MODEL=gemini-3.1-flash-tts-preview
#
# Remove the service when Demo Day is over:
#   gcloud run services delete anumaan-next --project anumaan-c4c --region asia-south1
