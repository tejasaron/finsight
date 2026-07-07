#!/usr/bin/env bash
# Deploy FinSight to Cloud Run using ADK's built-in deploy command.
#
# Prerequisites:
#   - gcloud CLI authenticated (`gcloud auth login`) with a project that has
#     the Cloud Run, Artifact Registry, and Secret Manager APIs enabled.
#   - The Gemini API key stored in Secret Manager (never baked into the
#     image or committed to source control):
#       echo -n "YOUR_KEY" | gcloud secrets create finsight-google-api-key --data-file=-
#
# Usage:
#   PROJECT_ID=your-gcp-project REGION=us-central1 ./deploy.sh

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID env var to your GCP project}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-finsight}"
SECRET_NAME="${SECRET_NAME:-finsight-google-api-key}"

adk deploy cloud_run \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service_name="${SERVICE_NAME}" \
  --app_name="finsight_agent" \
  --trace_to_cloud \
  ./finsight_agent

echo "Binding the Gemini API key from Secret Manager (never as a plain env var)..."
gcloud run services update "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --update-secrets="GOOGLE_API_KEY=${SECRET_NAME}:latest"

echo "Deployed. Fetching the service URL:"
gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)"
