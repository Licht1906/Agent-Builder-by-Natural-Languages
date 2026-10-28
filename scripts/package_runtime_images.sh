#!/usr/bin/env bash
# DS-19 — the images an exported package needs, as one signed `.tar`: what
# "Kèm images cho air-gap" puts in the zip. UC1-34's packaging
# (scripts/package_airgap_artifact.sh), cut down to the package's own images:
#
#   agent-runtime:latest     Dockerfile.runtime (the runtime + its chat page)
#   agent-reranker:latest    ./reranker
#   postgres:16-alpine, qdrant/qdrant:v1.12.4   the services docker-compose.yml names
#
# Runs on a *connected* build machine. Point the platform at the result:
#   PACKAGE_IMAGES_PATH=/srv/agent/dist/agent-runtime-images-<tag>.tar
# (the signature beside it, same name + .sig / .minisig, travels too).
#
# Usage:
#   COSIGN_KEY=./cosign.key ./scripts/package_runtime_images.sh [tag]
#   MINISIGN_SECRET_KEY=./minisign.key ./scripts/package_runtime_images.sh [tag]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=scripts/lib/shell_common.sh
source "${SCRIPT_DIR}/scripts/lib/shell_common.sh"

TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
OUT_DIR="$(pwd)/dist"
ARTIFACT_PATH="${OUT_DIR}/agent-runtime-images-${TAG}.tar"

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

log "Build agent-runtime (Dockerfile.runtime)"
docker build -f Dockerfile.runtime -t agent-runtime:latest -t "agent-runtime:${TAG}" .

log "Build agent-reranker"
docker build -t agent-reranker:latest -t "agent-reranker:${TAG}" ./reranker

# The tags docker-compose.yml of the package names — must stay in step with deploy/package/docker-compose.yml.
UPSTREAM_IMAGES=("postgres:16-alpine" "qdrant/qdrant:v1.12.4")
for image in "${UPSTREAM_IMAGES[@]}"; do
    log "Pull ${image}"
    docker pull "$image"
done

mkdir -p "$OUT_DIR"
log "docker save vào ${ARTIFACT_PATH} (có thể mất vài phút)"
docker save agent-runtime:latest agent-reranker:latest "${UPSTREAM_IMAGES[@]}" -o "$ARTIFACT_PATH"

log "Ký bằng ${SIGNER}"
case "$SIGNER" in
cosign) cosign sign-blob --key "$COSIGN_KEY" --output-signature "${ARTIFACT_PATH}.sig" --yes "$ARTIFACT_PATH" ;;
minisign) minisign -S -s "$MINISIGN_SECRET_KEY" -m "$ARTIFACT_PATH" -x "${ARTIFACT_PATH}.minisig" ;;
esac

cat <<EOF

============================================================
 Images: ${ARTIFACT_PATH}
 Đặt cho nền tảng:  PACKAGE_IMAGES_PATH=${ARTIFACT_PATH}
 Từ đó nút "Xuất gói chạy độc lập" cho tick "Kèm images cho air-gap".
============================================================
EOF
