#!/usr/bin/env bash
# The national layer of the BigQuery clean room (anumaan/cleanroom.py), once:
# a service account that can run queries and write the anumaan_national dataset,
# and nothing else. Each state grants it its privacy view when it onboards
#   python -m anumaan.cleanroom onboard S0
# and it never gets a role on a state's raw dataset.
#   bash deploy/national.sh
# Every call passes the project; this script never changes your gcloud config.
set -euo pipefail

PROJECT=anumaan-c4c
LOCATION=asia-south1
SA="anumaan-national@$PROJECT.iam.gserviceaccount.com"

gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create anumaan-national --project "$PROJECT" \
    --display-name "Anumaan national layer: state clean-room views only"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/bigquery.jobUser --condition None --quiet >/dev/null
# lets you act as the national layer: python -m anumaan.cleanroom proof
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" \
  --member "user:$(gcloud config get account)" --role roles/iam.serviceAccountTokenCreator --quiet >/dev/null

bq --project_id "$PROJECT" show anumaan_national >/dev/null 2>&1 ||
  bq --project_id "$PROJECT" --location "$LOCATION" mk --dataset \
    --description "SYNTHETIC. National layer: per-warehouse aggregates pulled through the state clean-room views" \
    "$PROJECT:anumaan_national"
bq --quiet --project_id "$PROJECT" --location "$LOCATION" query --nouse_legacy_sql \
  "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`$PROJECT\`.anumaan_national TO 'serviceAccount:$SA'" >/dev/null
echo "national layer: $SA"
