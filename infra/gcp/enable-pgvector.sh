#!/usr/bin/env bash
set -euo pipefail

# Bật extension `vector` trên Cloud SQL. Chạy một lần, sau setup.sh và TRƯỚC
# lần deploy đầu tiên — `alembic upgrade head` tạo cột kiểu `vector` (ADR-0045)
# và sẽ chết ngay ở migration đầu nếu extension chưa có.
#
# Vì sao phải vòng qua VM thay vì `gcloud sql connect`: instance tạo với
# --no-assign-ip nên không có public IP, mà `gcloud sql connect` lại hoạt động
# bằng cách whitelist IP client lên chính public IP đó. VM internal-services
# đã nằm trong VPC và nói chuyện được với private IP của Cloud SQL, nên nó là
# đường vào sẵn có — không cần dựng thêm bastion hay mở thêm gì.
#
# Usage: PROJECT_ID=xxx bash infra/gcp/enable-pgvector.sh

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-southeast1}"
ZONE="${ZONE:-${REGION}-a}"
SQL_INSTANCE="${SQL_INSTANCE:-app-db}"
SQL_DB="${SQL_DB:-app_prod}"
VM_NAME="${VM_NAME:-internal-services}"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== lấy thông tin kết nối =="
# Mật khẩu appuser chỉ tồn tại ở hai chỗ: DATABASE_URL trong Secret Manager
# (setup.sh đẩy lên) và bộ nhớ của lần chạy setup.sh đã kết thúc. Lấy từ chỗ đầu.
DB_URL=$(gcloud secrets versions access latest --secret=app-database-url)
DB_PASS=$(printf '%s' "$DB_URL" | sed -E 's|^postgresql://appuser:([^@]+)@.*$|\1|')
[ "$DB_PASS" != "$DB_URL" ] || { echo "không tách được mật khẩu từ app-database-url" >&2; exit 1; }

SQL_IP=$(gcloud sql instances describe "$SQL_INSTANCE" --format='get(ipAddresses[0].ipAddress)')
[ -n "$SQL_IP" ] || { echo "không lấy được private IP của $SQL_INSTANCE" >&2; exit 1; }
echo "   $SQL_INSTANCE @ $SQL_IP (private)"

echo "== CREATE EXTENSION vector, chạy từ trong VPC qua $VM_NAME =="
# Mật khẩu đi qua stdin của ssh chứ không nằm trên dòng lệnh, để nó không hiện
# trong `ps` trên VM. Phải có '\n' ở cuối: thiếu nó thì `read` gặp EOF và trả
# về non-zero dù đã gán biến xong, và `set -e` giết script ngay tại đó — không
# một dòng output nào, chỉ có exit code 1.
printf '%s\n' "$DB_PASS" | gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap --command="
  set -euo pipefail
  IFS= read -r pw
  sudo docker run --rm -i -e PGPASSWORD=\"\$pw\" postgres:15 \
    psql -h '$SQL_IP' -U appuser -d '$SQL_DB' \
      -v ON_ERROR_STOP=1 \
      -c 'CREATE EXTENSION IF NOT EXISTS vector;' \
      -c \"SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';\"
"

echo
echo "Xong. Giờ alembic upgrade head chạy được — push lên main để deploy.yml lo phần còn lại."
