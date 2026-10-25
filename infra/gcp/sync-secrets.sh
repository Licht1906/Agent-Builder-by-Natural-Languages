#!/usr/bin/env bash
set -euo pipefail

# Reads infra/gcp/.env.production (gitignored — copy .env.example, fill real
# values, leave the infra-wiring keys below blank) and creates/updates one
# Secret Manager secret per line, named app-<key-in-kebab-case>. deploy.yml's
# `--set-secrets` list expects exactly this naming.
#
# Usage: bash infra/gcp/sync-secrets.sh [path-to-env-file]

ENV_FILE="${1:-infra/gcp/.env.production}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE — copy .env.example there and fill it in" >&2; exit 1; }

# Set by deploy.yml directly from Cloud SQL/the internal-services VM/Cloud Run
# config, not from a secret — see setup.sh's printed DATABASE_URL/VAULT_TOKEN
# (already pushed to Secret Manager by setup.sh itself) and DEPLOYMENT.md.
#
# RERANKER_URL nằm trong danh sách này vì một lý do cứng hơn "không cần thiết":
# deploy.yml set nó bằng --update-env-vars sau khi biết URL Cloud Run thật của
# reranker, và Cloud Run TỪ CHỐI một key vừa là secret vừa là env var. Đồng bộ
# nó lên đây là làm mọi lần deploy fail.
#
# NETWORK_POLICY_HOST_ALIASES bị bỏ theo đúng phần "Known gaps" của
# DEPLOYMENT.md: giá trị trong .env.example map `smtp.internal.corp` sang
# `mailhog`, một service dev-only không tồn tại trên GCP.
SKIP_KEYS=" DATABASE_URL QDRANT_URL OPA_URL VAULT_URL VAULT_TOKEN SMTP_HOST APP_ENV CORS_ORIGINS FRONTEND_URL RERANKER_URL NETWORK_POLICY_HOST_ALIASES "

while IFS='=' read -r key value; do
  case "$key" in
    ""|\#*) continue ;;
  esac
  [ -z "$value" ] && continue
  case "$SKIP_KEYS" in *" $key "*) continue ;; esac

  secret_name="app-$(echo "$key" | tr '[:upper:]_' '[:lower:]-')"
  gcloud secrets describe "$secret_name" >/dev/null 2>&1 || \
    gcloud secrets create "$secret_name" --replication-policy=automatic >/dev/null
  printf '%s' "$value" | gcloud secrets versions add "$secret_name" --data-file=- >/dev/null
  echo "synced $secret_name"
done < "$ENV_FILE"
