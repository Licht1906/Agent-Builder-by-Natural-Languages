#!/usr/bin/env bash
# UC1-34 (#146), spec.md §12.3, NFR-05 — the import half of the air-gapped
# profile: "import script không cần bất kỳ kết nối ra ngoài nào". Every step
# below reads only from the artifact already on disk or from `docker load`;
# nothing here calls `docker pull`, `apt`, `pip`, or any URL.
#
# This script itself is *not* inside the `.tar` it operates on — verifying a
# signature only after already having extracted and trusted the thing it
# signs would defeat the point. `scripts/package_airgap_artifact.sh` does
# also copy a reference copy of this file (and `scripts/lib/`) into the
# artifact's own root purely for discoverability/backup, but the copy an
# operator actually *runs* has to arrive some other way — the same small,
# public, non-secret file distributed alongside the signed artifact and its
# `.sig`/`.minisig` (checked into this repo, carried over on removable media,
# whatever channel the artifact itself travelled on), never extracted from
# the artifact it is about to verify.
#
# Usage, on the disconnected machine — artifact, signature, and this script
# all sitting in the same directory:
#   ./import_airgap_artifact.sh agent-builder-airgap-<tag>.tar.sig <cosign-public-key>
#   # or, for a minisign-signed artifact:
#   ./import_airgap_artifact.sh agent-builder-airgap-<tag>.tar.minisig <minisign-public-key>
#
# The artifact `.tar` itself is expected alongside its signature (same
# directory, name minus the `.sig`/`.minisig` suffix) — pass the signature
# and the matching public key; the signature's own extension selects which
# tool this script verifies with, so there is only ever one flag to get
# right, not two. Everything this script needs after that (docker-compose.
# airgap.yml, images.tar, .env.example, scripts/lib/) comes out of the
# artifact itself, once — and only once — its signature has checked out.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# `log`/`die` are needed before the artifact is even verified, let alone
# extracted, so this sources the sibling copy expected next to this script
# itself (see module docstring) — never the one staged inside the artifact,
# which does not exist on disk yet at this point.
# shellcheck source=scripts/lib/shell_common.sh
source "${SCRIPT_DIR}/scripts/lib/shell_common.sh"

[[ $# -eq 2 ]] || die "cách dùng: $0 <artifact>.tar.sig|.minisig <public-key>"
SIGNATURE_PATH="$1"
PUBLIC_KEY_PATH="$2"

case "$SIGNATURE_PATH" in
*.sig) ARTIFACT_PATH="${SIGNATURE_PATH%.sig}" ;;
*.minisig) ARTIFACT_PATH="${SIGNATURE_PATH%.minisig}" ;;
*) die "chữ ký phải có đuôi .sig (cosign) hoặc .minisig (minisign), nhận được: ${SIGNATURE_PATH}" ;;
esac
[[ -f "$ARTIFACT_PATH" ]] || die "không tìm thấy artifact ${ARTIFACT_PATH} (đặt cạnh ${SIGNATURE_PATH})"

command -v docker >/dev/null 2>&1 || die "docker không có trong PATH"
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose -f docker-compose.airgap.yml)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose -f docker-compose.airgap.yml)
else
    die "không tìm thấy 'docker compose' hay 'docker-compose'"
fi

# --------------------------------------------------------------------------- #
# 1. Verify the signature — fail closed. Nothing below this point runs
#    against an artifact whose signature did not check out.
# --------------------------------------------------------------------------- #

log "Xác minh chữ ký"
case "$SIGNATURE_PATH" in
*.sig)
    command -v cosign >/dev/null 2>&1 || die "cần 'cosign' trong PATH để xác minh"
    cosign verify-blob --key "$PUBLIC_KEY_PATH" --signature "$SIGNATURE_PATH" "$ARTIFACT_PATH" \
        || die "chữ ký cosign KHÔNG hợp lệ — dừng lại, không import artifact chưa xác minh được"
    ;;
*.minisig)
    command -v minisign >/dev/null 2>&1 || die "cần 'minisign' trong PATH để xác minh"
    minisign -V -p "$PUBLIC_KEY_PATH" -m "$ARTIFACT_PATH" -x "$SIGNATURE_PATH" \
        || die "chữ ký minisign KHÔNG hợp lệ — dừng lại, không import artifact chưa xác minh được"
    ;;
esac
log "Chữ ký hợp lệ"

# --------------------------------------------------------------------------- #
# 2. Extract (idempotent: re-running over an already-extracted artifact just
#    overwrites the same files with themselves) and load images
# --------------------------------------------------------------------------- #

log "Giải nén artifact"
EXTRACT_DIR="${ARTIFACT_PATH%.tar}"
mkdir -p "$EXTRACT_DIR"
tar -xf "$ARTIFACT_PATH" -C "$EXTRACT_DIR"
cd "$EXTRACT_DIR"

log "docker load images.tar (không cần mạng ngoài, có thể mất vài phút)"
docker load -i images.tar

export AGENT_BUILDER_IMAGE_TAG
AGENT_BUILDER_IMAGE_TAG="$(cat IMAGE_TAG)"

# --------------------------------------------------------------------------- #
# 3. .env + secrets — same logic install.sh (#145) uses
# --------------------------------------------------------------------------- #

ENV_FILE=".env"
if [[ ! -f "$ENV_FILE" ]]; then
    log "Tạo $ENV_FILE từ .env.example"
    cp .env.example "$ENV_FILE"
fi

# shellcheck source=scripts/lib/env_secrets.sh
source "./scripts/lib/env_secrets.sh"
_ensure_secret JWT_SECRET "dev-only-insecure-secret-change-me"
_ensure_secret SECRET_STORE_KEY "dev-only-insecure-secret-store-key-change-me"

# --------------------------------------------------------------------------- #
# 4. Migrate (also seeds agent_admin — migration d13a1d1e5b78), then start
# --------------------------------------------------------------------------- #

log "Khởi động Postgres"
"${COMPOSE[@]}" up -d db
log "Chờ Postgres sẵn sàng"
wait_for_postgres db "${COMPOSE[@]}"

log "Chạy migration (alembic upgrade head)"
"${COMPOSE[@]}" run --rm backend alembic upgrade head

log "Khởi động toàn bộ service"
"${COMPOSE[@]}" up -d

SEED_EMAIL="$(grep '^SEED_AGENT_ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2-)"
SEED_PASSWORD="$(grep '^SEED_AGENT_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"

cat <<EOF

============================================================
 Import xong (ngoại tuyến hoàn toàn — không có lệnh nào ở trên gọi ra mạng).

 Tài khoản agent_admin đầu tiên (seed):
   email:    ${SEED_EMAIL:-agent-admin@example.com}
   password: ${SEED_PASSWORD:-agent-admin-dev-password}

 API + Swagger UI:  http://localhost:8000/docs
============================================================
EOF
