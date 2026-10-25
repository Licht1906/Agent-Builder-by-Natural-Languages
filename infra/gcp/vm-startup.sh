#!/usr/bin/env bash
set -euo pipefail

# Metadata startup-script for the `internal-services` VM (see setup.sh and
# ../../DEPLOYMENT.md). Runs on every boot. Installs Docker once, mounts the
# attached persistent disk at /mnt/data once, and (re)starts the
# qdrant/policy/vault containers if the compose file has already been pushed
# — it hasn't yet on the VM's first boot, setup.sh pushes it right after
# creating the instance and starts the containers itself over SSH.

if ! command -v docker >/dev/null 2>&1; then
  # Không prompt: script này chạy không có TTY, cả lúc boot lẫn khi chạy lại
  # bằng `google_metadata_script_runner startup`.
  export DEBIAN_FRONTEND=noninteractive

  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings

  # Lưu key ở dạng ASCII-armored (.asc) và để apt tự xử lý, thay vì
  # `curl … | gpg --dearmor`. gpg mở /dev/tty ngay cả cho --dearmor, mà startup
  # script thì không có tty, nên đường ống kia chết bằng
  # `gpg: cannot open '/dev/tty'` rồi kéo theo `curl: (23) Failed writing body`
  # — không dấu hiệu nào chỉ về phía tty. apt từ Debian 11 trở đi đọc trực
  # tiếp .asc trong signed-by, nên bỏ hẳn gpg là cách sửa gọn nhất.
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

if ! mountpoint -q /mnt/data; then
  DEV=/dev/disk/by-id/google-data
  mkdir -p /mnt/data
  blkid "$DEV" || mkfs.ext4 -m 0 -F "$DEV"
  mount -o discard,defaults "$DEV" /mnt/data
  grep -q /mnt/data /etc/fstab || echo "$DEV /mnt/data ext4 discard,defaults,nofail 0 2" >> /etc/fstab
fi
mkdir -p /mnt/data/qdrant

if [ -f /opt/app/docker-compose.yml ]; then
  cd /opt/app
  docker compose up -d
fi
