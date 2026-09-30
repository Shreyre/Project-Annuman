#!/usr/bin/env bash
# Deploy Anumaan to Cloud Run in Mumbai, with Gemini on Vertex AI for voice.
#   bash deploy/deploy.sh
# Run deploy/budget.sh first. min-instances 0: scales to zero when idle, so the first
# request after a quiet spell waits for a cold start. max-instances 1: shelf checks, approved
# transfers and the live feed are held in the instance's memory, so a second instance would
# answer from a different state. deploy/live.sh then puts the live feed on Pub/Sub.
# Every call passes --project; this script never changes your gcloud config.
set -euo pipefail

PROJECT=anumaan-c4c
REGION=asia-south1
MODEL=gemini-3.7-flash
SA_NAME=anumaan-run
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"

cd "$(dirname "$0")/.."

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com aiplatform.googleapis.com --project "$PROJECT"

# runtime identity: may call Gemini, nothing else
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --display-name "Anumaan Cloud Run runtime" --project "$PROJECT"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/aiplatform.user --condition None --quiet >/dev/null

# builds with Cloud Build from the Dockerfile; .env is excluded (.gitignore / .dockerignore).
# On a brand-new project the first build can fail on permissions while IAM propagates:
# wait a minute and re-run. If it keeps failing, grant roles/run.builder to
# PROJECT_NUMBER-compute@developer.gserviceaccount.com.
gcloud run deploy anumaan --source . --project "$PROJECT" --region "$REGION" \
  --service-account "$SA" --min-instances 0 --max-instances 1 --memory 1Gi \
  --allow-unauthenticated \
  --update-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=global,ANUMAAN_MODEL=$MODEL"

gcloud run services describe anumaan --project "$PROJECT" --region "$REGION" --format 'value(status.url)'
