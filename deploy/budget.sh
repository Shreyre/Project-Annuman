#!/usr/bin/env bash
# Budget alerts for anumaan-c4c at 50%, 90% and 100% of a monthly amount.
#   bash deploy/budget.sh BILLING_ACCOUNT_ID AMOUNT [CURRENCY]
#   bash deploy/budget.sh 0X0X0X-0X0X0X-0X0X0X 2000 INR
# (gcloud billing accounts list shows the id). A budget only sends alerts to billing
# admins; it does NOT stop spending. min-instances=1 bills around the clock.
set -euo pipefail

BILLING=${1:?usage: budget.sh BILLING_ACCOUNT_ID AMOUNT [CURRENCY]}
AMOUNT=${2:?usage: budget.sh BILLING_ACCOUNT_ID AMOUNT [CURRENCY]}
CURRENCY=${3:-INR}     # must be the billing account's currency
PROJECT=anumaan-c4c

gcloud services enable billingbudgets.googleapis.com --project "$PROJECT"
gcloud billing budgets create --project "$PROJECT" --billing-account "$BILLING" \
  --display-name "anumaan-c4c monthly" --budget-amount "$AMOUNT$CURRENCY" \
  --filter-projects "projects/$PROJECT" \
  --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0
