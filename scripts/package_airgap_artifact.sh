#!/usr/bin/env bash
# UC1-34 (#146), spec.md §12.3, NFR-05 — packages this repo's air-gapped
# profile into one signed `.tar`: "khách hàng import artifact offline dạng
# .tar đã ký (cosign/minisign), không cần bất kỳ kết nối ra ngoài nào."
#
# Runs on a *connected* build machine (this step needs the network — `docker
# build` pulling base images, `pip install`). Its output is what
# `import_airgap_artifact.sh` later applies on a machine with none at all.
#
# What goes in the artifact:
#   - images.tar        `docker save` of every image docker-compose.airgap.yml
#                        names, so the target machine never runs `docker pull`.
#   - docker-compose.airgap.yml, .env.example
#   - docs/usecase_spec/platform/   (the `policy` service's own bind mount —
#                                    baked into no image, so it has to travel
#                                    as files)
#   - scripts/lib/env_secrets.sh, scripts/lib/shell_common.sh,
#     scripts/import_airgap_artifact.sh
#
# Usage:
#   COSIGN_KEY=./cosign.key ./scripts/package_airgap_artifact.sh [tag]
#   MINISIGN_SECRET_KEY=./minisign.key ./scripts/package_airgap_artifact.sh [tag]
#
# Exactly one of COSIGN_KEY / MINISIGN_SECRET_KEY must be set, naming a
# private key this script does not generate — key provisioning is a release
# process decision, not something a packaging run should mint casually.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
ARTIFACT_NAME="agent-builder-airgap-${TAG}"
OUT_DIR="$(pwd)/dist"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

# shellcheck source=scripts/lib/shell_common.sh
source "${SCRIPT_DIR}/scripts/lib/shell_common.sh"

command -v docker >/dev/null 2>&1 || die "docker không có trong PATH"

if [[ -n "${COSIGN_KEY:-}" ]]; then
    command -v cosign >/dev/null 2>&1 || die "COSIGN_KEY được đặt nhưng không tìm thấy 'cosign' trong PATH"
    SIGNER="cosign"
elif [[ -n "${MINISIGN_SECRET_KEY:-}" ]]; then
    command -v minisign >/dev/null 2>&1 || die "MINISIGN_SECRET_KEY được đặt nhưng không tìm thấy 'minisign' trong PATH"
    SIGNER="minisign"
else
    die "cần đặt đúng một trong COSIGN_KEY hoặc MINISIGN_SECRET_KEY (đường dẫn tới private key) trước khi chạy"
fi

# --------------------------------------------------------------------------- #
# 1. Build + tag every image docker-compose.airgap.yml names
# --------------------------------------------------------------------------- #

log "Build backend:${TAG}"
docker build -t "agent-builder/backend:${TAG}" .

log "Build reranker:${TAG}"
docker build -t "agent-builder/reranker:${TAG}" ./reranker

# Pinned upstream images — pulled once, saved alongside the two this repo
# builds so `docker load` on the target machine has all of them, not only
# the two that are actually built here.
UPSTREAM_IMAGES=(
    "postgres:16-alpine"
    "qdrant/qdrant:v1.12.4"
    "openpolicyagent/opa:1.19.0"
    "hashicorp/vault:1.18"
    "mailhog/mailhog:v1.0.1"
)
for image in "${UPSTREAM_IMAGES[@]}"; do
    log "Pull ${image}"
    docker pull "$image"
done

# --------------------------------------------------------------------------- #
# 2. Stage the artifact
# --------------------------------------------------------------------------- #

log "Đóng gói vào ${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/docs/usecase_spec" "${STAGE_DIR}/scripts/lib"
cp docker-compose.airgap.yml "$STAGE_DIR/"
cp .env.example "$STAGE_DIR/"
cp scripts/lib/env_secrets.sh scripts/lib/shell_common.sh "${STAGE_DIR}/scripts/lib/"
cp scripts/import_airgap_artifact.sh "$STAGE_DIR/"
cp -r docs/usecase_spec/platform "$STAGE_DIR/docs/usecase_spec/platform"

log "docker save toàn bộ image vào images.tar (có thể mất vài phút)"
docker save \
    "agent-builder/backend:${TAG}" \
    "agent-builder/reranker:${TAG}" \
    "${UPSTREAM_IMAGES[@]}" \
    -o "${STAGE_DIR}/images.tar"

echo "$TAG" >"${STAGE_DIR}/IMAGE_TAG"

mkdir -p "$OUT_DIR"
ARTIFACT_PATH="${OUT_DIR}/${ARTIFACT_NAME}.tar"
log "Gộp thành ${ARTIFACT_PATH}"
tar -cf "$ARTIFACT_PATH" -C "$STAGE_DIR" .

# --------------------------------------------------------------------------- #
# 3. Sign
# --------------------------------------------------------------------------- #

log "Ký artifact bằng ${SIGNER}"
case "$SIGNER" in
cosign)
    cosign sign-blob --key "$COSIGN_KEY" --output-signature "${ARTIFACT_PATH}.sig" --yes "$ARTIFACT_PATH"
    ;;
minisign)
    minisign -S -s "$MINISIGN_SECRET_KEY" -m "$ARTIFACT_PATH" -x "${ARTIFACT_PATH}.minisig"
    ;;
esac

cat <<EOF

============================================================
 Artifact: ${ARTIFACT_PATH}
 Chữ ký:   ${ARTIFACT_PATH}.$([ "$SIGNER" = cosign ] && echo sig || echo minisig)

 Trên máy đích (không cần mạng ngoài):
   ./import_airgap_artifact.sh ${ARTIFACT_NAME}.tar <public-key>
============================================================
EOF
