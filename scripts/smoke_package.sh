#!/usr/bin/env bash
# DS-19 smoke test — the ticket's own words: "giải nén zip + điền secret + docker compose up → agent chạy".
#
#   ./scripts/smoke_package.sh            # builds agent-runtime:latest first
#   SKIP_BUILD=1 ./scripts/smoke_package.sh
#
# 1. assemble a package zip the way the export does (scripts/build_smoke_package.py)
# 2. unzip it, write .env with the secrets its .env.example names
# 3. docker compose up — runtime + its Postgres + its Qdrant
# 4. the agent answers: /health ok, the chat page is served, /chat refuses nobody and streams for an asker
# 5. docker compose down -v, whatever happened

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/lib/shell_common.sh
source "${ROOT}/scripts/lib/shell_common.sh"

command -v docker >/dev/null 2>&1 || die "docker không có trong PATH"
PYTHON="${PYTHON:-python}"
PORT="${SMOKE_PORT:-18080}"
PROJECT="agent-package-smoke"
WORK="$(mktemp -d)"

cleanup() {
    if [[ -d "${WORK}/pkg" ]]; then
        (cd "${WORK}/pkg" && docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1) || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

if [[ -z "${SKIP_BUILD:-}" ]]; then
    log "Build agent-runtime:latest (Dockerfile.runtime)"
    docker build -f Dockerfile.runtime -t agent-runtime:latest .
fi

log "Ghép gói mẫu"
PYTHONPATH="$ROOT" "$PYTHON" scripts/build_smoke_package.py "${WORK}/package.zip"

log "Giải nén và điền secret"
mkdir -p "${WORK}/pkg"
"$PYTHON" -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "${WORK}/package.zip" "${WORK}/pkg"
cd "${WORK}/pkg"
for required in bundle.json runtime.yaml docker-compose.yml .env.example README.md; do
    [[ -f "$required" ]] || die "gói thiếu ${required}"
done
# Every AGENT_SECRET_* line .env.example names gets a value — a secret the smoke forgot would stop the start, by name.
{
    echo "AGENT_DB_PASSWORD=smoke-db-password"
    echo "AGENT_PORT=${PORT}"
    grep -E '^AGENT_SECRET_[A-Z0-9_]+=' .env.example | cut -d= -f1 | while read -r name; do
        case "$name" in
        AGENT_SECRET_RUNTIME_DB) echo "${name}=postgresql://agent:smoke-db-password@postgres:5432/agent" ;;
        *) echo "${name}=smoke-placeholder" ;;
        esac
    done
} >.env

log "docker compose up"
docker compose -p "$PROJECT" up -d

log "Chờ /health"
healthy=""
for _ in $(seq 1 90); do
    if curl -fsS "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"status":"ok"'; then
        healthy=1
        break
    fi
    sleep 2
done
if [[ -z "$healthy" ]]; then
    docker compose -p "$PROJECT" logs runtime || true
    die "gói không lên /health trong 180 giây"
fi

log "Kiểm agent"
curl -fsS "http://127.0.0.1:${PORT}/ui-config" | grep -q '"agent":"Tro ly kiem thu goi"' || die "/ui-config không nêu tên agent"
curl -fsS "http://127.0.0.1:${PORT}/" | grep -q 'id="root"' || die "trang chat không được phục vụ ở /"
status="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/chat" -H 'Content-Type: application/json' -d '{"message":"xin chao"}')"
[[ "$status" == "401" ]] || die "/chat không có người hỏi phải là 401, nhận ${status}"
# Read whole, then searched: `curl | grep -q` would close the pipe on the first match and fail under pipefail.
# No LLM is reachable here, so the run ends in an `error` event — what matters is that a run opened for the asker.
streamed="$(curl -sS -N --max-time 30 -X POST "http://127.0.0.1:${PORT}/chat" -H 'Content-Type: application/json' \
    -H 'X-Asker: an@cty.vn' -d '{"message":"xin chao"}' || true)"
grep -q '^event: thread' <<<"$streamed" || die "/chat không mở được lượt chạy: ${streamed:0:300}"

log "Gói chạy: /health ok, trang chat, /chat 401 khi không có người hỏi, mở lượt chạy khi có"
