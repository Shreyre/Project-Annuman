#!/usr/bin/env bash
# The live feed on Google Cloud: a Pub/Sub topic the service publishes each PHC's day to, and a
# push subscription that delivers every message back to /api/ingest.
#   bash deploy/live.sh        # once, after deploy/deploy.sh; run again to rotate the token
# Without it the Live feed still runs: the same messages are applied in-process, and the page says so.
# Cost: Pub/Sub's first 10 GB a month are free; a day of the demo network is about 80 KB.
# Every call passes --project; this script never changes your gcloud config.
set -euo pipefail

PROJECT=anumaan-c4c
REGION=asia-south1
TOPIC=anumaan-feed
SA="anumaan-run@$PROJECT.iam.gserviceaccount.com"

gcloud services enable pubsub.googleapis.com --project "$PROJECT"
URL=$(gcloud run services describe anumaan --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

gcloud pubsub topics describe "$TOPIC" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud pubsub topics create "$TOPIC" --project "$PROJECT"
# the runtime identity may publish to this one topic and nothing else in Pub/Sub
gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/pubsub.publisher >/dev/null

# /api/ingest lets a message in only with the token in the push URL. Messages are kept 10 minutes,
# so nothing from an earlier run arrives after a restart.
# ponytail: the token sits in the subscription and the service's environment, readable by anyone who can
# view the project; for a state's real feed use an OIDC-authenticated push and Secret Manager.
gcloud pubsub subscriptions delete "$TOPIC-push" --project "$PROJECT" --quiet >/dev/null 2>&1 || true
gcloud pubsub subscriptions create "$TOPIC-push" --project "$PROJECT" --topic "$TOPIC" \
  --push-endpoint "$URL/api/ingest?token=$TOKEN" --ack-deadline 30 \
  --message-retention-duration 10m --expiration-period never

gcloud run services update anumaan --project "$PROJECT" --region "$REGION" \
  --update-env-vars "ANUMAAN_PUBSUB_TOPIC=projects/$PROJECT/topics/$TOPIC,ANUMAAN_INGEST_TOKEN=$TOKEN"
echo "Live feed: $URL/?net=live now sends its reports through projects/$PROJECT/topics/$TOPIC"
