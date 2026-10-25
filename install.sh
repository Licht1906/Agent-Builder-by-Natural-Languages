#!/usr/bin/env bash
# UC1-33 (#145), spec.md §12.1/§12.2, NFR-04 — "git clone && ./install.sh &&
# docker compose up -d" in under 15 minutes on a clean machine.
#
# What this script does, in the order §12.2 lists them:
#   1. Generate .env (from .env.example) if one does not exist yet.
#   2. Generate real secrets for JWT_SECRET/SECRET_STORE_KEY — the "master
#      key" step — replacing the checked-in dev-only defaults, which
#      src/config.py's own Settings refuses outright in production and which
#      nobody should run a real install on regardless.
#   3. Bring up just the database, migrate it (alembic upgrade head), which
#      is also where the first agent_admin account gets seeded (migration
#      d13a1d1e5b78 — "seed admin" is not a separate script to run).
#   4. Bring up every other service (docker-compose.yml's own §6.2 table).
#
# Idempotent: re-running after a partial or repeat install never overwrites
# an operator-customised secret (only ever fills in the placeholder this
# script itself would have generated), and every docker/alembic step here is
# itself idempotent (`CREATE TABLE IF NOT EXISTS`, `docker compose up` on an
# already-running stack).
#
# NOTE on "màn hình đăng ký" (§12.2's own last line): the UC1 web UI
# (§6.2's `web`/Next.js row) is a separate, still-unbuilt ticket (E2,
# `src.api.uc1_approval_routes`'s own docstring on `_FORM_CONFIRMED_STAND_IN`
# names the same gap) — `frontend/` in this repo is a different, V2 app, not
# wired into docker-compose.yml at all. Until UC1's own web UI ships, the
# honest browser-visible landing point this script prints is FastAPI's own
# `/docs` — where `POST /auth/register` can be exercised directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

# shellcheck source=scripts/lib/shell_common.sh
source "${SCRIPT_DIR}/scripts/lib/shell_common.sh"

# --------------------------------------------------------------------------- #
# 0. Prerequisites
# --------------------------------------------------------------------------- #

command -v docker >/dev/null 2>&1 || die "docker không có trong PATH — cài Docker trước (https://docs.docker.com/get-docker/)"
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "không tìm thấy 'docker compose' hay 'docker-compose' — cài Docker Compose trước"
fi

# --------------------------------------------------------------------------- #
# 1. .env
# --------------------------------------------------------------------------- #

if [[ ! -f "$ENV_FILE" ]]; then
    log "Tạo $ENV_FILE từ $ENV_EXAMPLE"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
else
    log "$ENV_FILE đã tồn tại — giữ nguyên, chỉ điền secret còn thiếu/mặc định"
fi

# --------------------------------------------------------------------------- #
# 2. Master key / secrets — shared with import_airgap_artifact.sh (#146) so
# both install paths judge "still the checked-in default" the same way.
# --------------------------------------------------------------------------- #

# shellcheck source=scripts/lib/env_secrets.sh
source "${SCRIPT_DIR}/scripts/lib/env_secrets.sh"

_ensure_secret JWT_SECRET "dev-only-insecure-secret-change-me"
_ensure_secret SECRET_STORE_KEY "dev-only-insecure-secret-store-key-change-me"

# --------------------------------------------------------------------------- #
# 3. Database: up, migrate (also seeds agent_admin — migration d13a1d1e5b78)
# --------------------------------------------------------------------------- #

log "Khởi động Postgres"
"${COMPOSE[@]}" up -d db

log "Chờ Postgres sẵn sàng"
wait_for_postgres db "${COMPOSE[@]}"

log "Chạy migration (alembic upgrade head) — seed agent_admin nằm trong bước này"
"${COMPOSE[@]}" run --rm backend alembic upgrade head

# --------------------------------------------------------------------------- #
# 4. Everything else
# --------------------------------------------------------------------------- #

log "Khởi động toàn bộ service"
"${COMPOSE[@]}" up -d

SEED_EMAIL="$(grep '^SEED_AGENT_ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2-)"
SEED_PASSWORD="$(grep '^SEED_AGENT_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"

cat <<EOF

============================================================
 Cài đặt xong.

 Tài khoản agent_admin đầu tiên (seed):
   email:    ${SEED_EMAIL:-agent-admin@example.com}
   password: ${SEED_PASSWORD:-agent-admin-dev-password}

 API + Swagger UI:  http://localhost:8000/docs
 MailHog (email đi vào):  http://localhost:8025

 Đổi mật khẩu tài khoản seed ngay sau lần đăng nhập đầu tiên.
============================================================
EOF
