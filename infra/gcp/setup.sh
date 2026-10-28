#!/usr/bin/env bash
set -euo pipefail

# One-time GCP infra bootstrap for Agent Builder by Natural Languages's production deploy. Run once from a
# workstation with `gcloud auth login` done and Owner/Editor on the target
# project; safe to re-run (every step checks "does this already exist" first)
# if it fails partway through. See ../../DEPLOYMENT.md for the architecture
# this builds and what runs where.
#
# Usage: PROJECT_ID=xxx GITHUB_REPO=org/repo bash infra/gcp/setup.sh

cd "$(dirname "$0")/../.."  # repo root, so the relative paths below hold

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-southeast1}"
ZONE="${ZONE:-${REGION}-a}"
GITHUB_REPO="${GITHUB_REPO:?set GITHUB_REPO, e.g. Licht1906/Agent-Builder-by-Natural-Languages}"

VPC_NAME=app-vpc
SUBNET_NAME=app-subnet
SUBNET_RANGE=10.10.0.0/20
ROUTER_NAME=app-router
NAT_NAME=app-nat
SQL_INSTANCE=app-db
SQL_DB=app_prod
VM_NAME=internal-services

# Mặc định dưới đây đã hạ theo mô hình "service chỉ available 12/24" —
# xem phần "Chi phí" trong DEPLOYMENT.md để biết đánh đổi của từng dòng.
# Ghi đè bằng env var nếu cần chạy nặng hơn (vd SQL_AVAILABILITY=REGIONAL).
SQL_TIER="${SQL_TIER:-db-custom-1-3840}"
SQL_AVAILABILITY="${SQL_AVAILABILITY:-ZONAL}"
VM_MACHINE_TYPE="${VM_MACHINE_TYPE:-e2-medium}"
VM_DISK_SIZE="${VM_DISK_SIZE:-30GB}"
VM_DISK_TYPE="${VM_DISK_TYPE:-pd-balanced}"

# Serverless VPC Access connector cũ, không còn dùng: Cloud Run nối thẳng vào
# VPC bằng Direct VPC egress. Tên này chỉ còn để phát hiện và dọn bản cũ.
LEGACY_CONNECTOR_NAME=app-connector
AR_REPO=app
RUN_SA=app-run-sa
DEPLOY_SA=gh-actions-deployer
WIF_POOL=github-pool
WIF_PROVIDER=github-provider

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== enabling APIs =="
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com compute.googleapis.com \
  vpcaccess.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com servicenetworking.googleapis.com \
  iamcredentials.googleapis.com cloudresourcemanager.googleapis.com

echo "== Artifact Registry =="
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION"

echo "== VPC + subnet =="
gcloud compute networks describe "$VPC_NAME" >/dev/null 2>&1 || \
  gcloud compute networks create "$VPC_NAME" --subnet-mode=custom

gcloud compute networks subnets describe "$SUBNET_NAME" --region="$REGION" >/dev/null 2>&1 || \
  gcloud compute networks subnets create "$SUBNET_NAME" \
    --network="$VPC_NAME" --region="$REGION" --range="$SUBNET_RANGE"

echo "== private services access (Cloud SQL private IP) =="
gcloud compute addresses describe "google-managed-services-$VPC_NAME" --global >/dev/null 2>&1 || \
  gcloud compute addresses create "google-managed-services-$VPC_NAME" \
    --global --purpose=VPC_PEERING --prefix-length=16 --network="$VPC_NAME"

gcloud services vpc-peerings connect \
  --service=servicenetworking.googleapis.com \
  --ranges="google-managed-services-$VPC_NAME" \
  --network="$VPC_NAME" || true

# ZONAL chứ không REGIONAL: HA hai zone nhân đôi tiền vCPU/RAM để mua lấy
# failover tự động, mà một hệ thống cố tình tắt 12 tiếng mỗi đêm thì đằng nào
# cũng không hứa uptime liên tục. Backup hằng ngày vẫn còn — mất zone là mất
# thời gian khôi phục, không mất dữ liệu. Đặt SQL_AVAILABILITY=REGIONAL để
# quay lại nếu bài toán đổi.
echo "== Cloud SQL for PostgreSQL (private IP, $SQL_AVAILABILITY, $SQL_TIER) =="
gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1 || \
  gcloud sql instances create "$SQL_INSTANCE" \
    --database-version=POSTGRES_15 --tier="$SQL_TIER" --region="$REGION" \
    --network="$VPC_NAME" --no-assign-ip \
    --backup-start-time=18:00 --availability-type="$SQL_AVAILABILITY"

gcloud sql databases describe "$SQL_DB" --instance="$SQL_INSTANCE" >/dev/null 2>&1 || \
  gcloud sql databases create "$SQL_DB" --instance="$SQL_INSTANCE"

DB_PASSWORD=$(openssl rand -hex 24)
gcloud sql users create appuser --instance="$SQL_INSTANCE" --password="$DB_PASSWORD" 2>/dev/null || \
  gcloud sql users set-password appuser --instance="$SQL_INSTANCE" --password="$DB_PASSWORD"

SQL_CONNECTION_NAME=$(gcloud sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')
DATABASE_URL="postgresql://appuser:${DB_PASSWORD}@/${SQL_DB}?host=/cloudsql/${SQL_CONNECTION_NAME}"

# `gcloud sql connect` KHÔNG dùng được ở đây: nó chạy bằng cách whitelist IP
# client lên public IP của instance, mà instance này tạo với --no-assign-ip.
# Đường vào duy nhất là từ trong VPC — VM internal-services đã nằm sẵn ở đó.
# Dùng appuser chứ không phải postgres: user tạo qua Cloud SQL API được cấp
# cloudsqlsuperuser, đủ quyền CREATE EXTENSION cho các extension được hỗ trợ,
# còn `postgres` thì script này chưa từng đặt mật khẩu.
echo ">> One-time manual step, before the first deploy: pgvector needs the extension created."
echo ">>   bash infra/gcp/enable-pgvector.sh"
echo ">>   (plain Postgres has no 'vector' type until this runs — same reason docker-compose.yml uses"
echo ">>    the pgvector/pgvector image locally instead of stock postgres.)"

# Không dựng Serverless VPC Access connector nữa: nó chạy tối thiểu hai
# instance e2-micro, tính tiền 24/24 và không scale-to-zero được — tức là
# đúng loại chi phí mà cả cái lịch bật/tắt này sinh ra để loại bỏ. Cloud Run
# dùng Direct VPC egress thay thế (deploy.yml: --network/--subnet), không có
# thành phần nào phải trả tiền lúc rảnh.
if gcloud compute networks vpc-access connectors describe "$LEGACY_CONNECTOR_NAME" --region="$REGION" >/dev/null 2>&1; then
  echo ">> Còn connector cũ '$LEGACY_CONNECTOR_NAME' đang tính tiền 24/24."
  echo ">> Xoá SAU khi deploy.yml đã chạy một lần với Direct VPC egress:"
  echo ">>   gcloud compute networks vpc-access connectors delete $LEGACY_CONNECTOR_NAME --region=$REGION"
fi

# Nguồn là cả subnet chứ không phải dải riêng của connector: với Direct VPC
# egress, instance Cloud Run lấy IP từ chính "$SUBNET_NAME".
# VM `internal-services` cố ý không có external IP, và điều đó có một hệ quả
# dễ bỏ sót: nó cũng không có đường ra Internet. vm-startup.sh cần
# `apt-get update` cùng GPG key từ download.docker.com để cài Docker, còn
# `docker compose up` cần kéo image qdrant từ Docker Hub — cả hai
# đều chết câm nếu thiếu NAT, và triệu chứng lộ ra tận sau, ở SSH kế tiếp,
# dưới dạng `sudo: docker: command not found`.
#
# Private Google Access không thay thế được: nó chỉ mở API của Google, không
# mở deb.debian.org hay Docker Hub. Cloud NAT là egress-only — không mở thêm
# đường vào nào — và tính tiền theo VM-giờ, nên VM tắt ban đêm thì gần như
# không mất gì (~$1/tháng cho một VM).
echo "== Cloud Router + Cloud NAT (VM không có external IP nhưng vẫn cần kéo package/image) =="
gcloud compute routers describe "$ROUTER_NAME" --region="$REGION" >/dev/null 2>&1 || \
  gcloud compute routers create "$ROUTER_NAME" --network="$VPC_NAME" --region="$REGION"

gcloud compute routers nats describe "$NAT_NAME" --router="$ROUTER_NAME" --region="$REGION" >/dev/null 2>&1 || \
  gcloud compute routers nats create "$NAT_NAME" \
    --router="$ROUTER_NAME" --region="$REGION" \
    --nat-all-subnet-ip-ranges --auto-allocate-nat-external-ips

echo "== firewall =="
gcloud compute firewall-rules describe allow-connector-to-internal >/dev/null 2>&1 || \
  gcloud compute firewall-rules create allow-connector-to-internal \
    --network="$VPC_NAME" --direction=INGRESS --action=ALLOW \
    --rules=tcp:6333,tcp:6334 \
    --source-ranges="$SUBNET_RANGE" --target-tags=internal-services

gcloud compute firewall-rules describe allow-iap-ssh >/dev/null 2>&1 || \
  gcloud compute firewall-rules create allow-iap-ssh \
    --network="$VPC_NAME" --direction=INGRESS --action=ALLOW \
    --rules=tcp:22 --source-ranges=35.235.240.0/20

echo "== data disk + internal-services VM (Qdrant, no external IP) =="
# Disk tính tiền cả khi VM đã tắt, nên nó là phần duy nhất của VM mà lịch
# bật/tắt không giảm được — vì vậy pd-balanced 30GB thay cho pd-ssd 50GB.
gcloud compute disks describe internal-services-data --zone="$ZONE" >/dev/null 2>&1 || \
  gcloud compute disks create internal-services-data --zone="$ZONE" --size="$VM_DISK_SIZE" --type="$VM_DISK_TYPE"

gcloud compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1 || \
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" --machine-type="$VM_MACHINE_TYPE" \
    --network="$VPC_NAME" --subnet="$SUBNET_NAME" --no-address \
    --tags=internal-services \
    --image-family=debian-12 --image-project=debian-cloud \
    --disk=name=internal-services-data,device-name=data,mode=rw,boot=no \
    --metadata-from-file startup-script=infra/gcp/vm-startup.sh

# `create` ở trên bỏ qua khi VM đã tồn tại, nhưng startup script nằm trong
# metadata của instance — sửa infra/gcp/vm-startup.sh trong repo không tự động
# tới được VM đã dựng. Đẩy lại mỗi lần chạy để hai bên không trôi khỏi nhau.
echo "== đồng bộ startup script vào metadata của VM =="
gcloud compute instances add-metadata "$VM_NAME" --zone="$ZONE" \
  --metadata-from-file startup-script=infra/gcp/vm-startup.sh >/dev/null

echo "== waiting for the VM to accept SSH over IAP =="
for i in $(seq 1 30); do
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap --command="true" 2>/dev/null && break
  sleep 10
done

# SSH mở trước khi vm-startup.sh cài xong Docker, nên chờ SSH là chưa đủ —
# `docker compose up` ngay sau đây sẽ báo `docker: command not found`.
# Chờ chính thứ mình cần thay vì chờ một dấu hiệu gần đúng.
vm_has_docker() {
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap \
    --command="command -v docker >/dev/null" 2>/dev/null
}

echo "== waiting for vm-startup.sh to install Docker =="
for i in $(seq 1 30); do
  vm_has_docker && break
  sleep 10
done

if ! vm_has_docker; then
  # Tới đây thường là VM đã boot xong từ trước khi Cloud NAT tồn tại: startup
  # script chạy một lần lúc boot, chết vì không có mạng, và không tự chạy lại.
  # Giờ đã có NAT thì chạy lại tay là đủ, không cần tạo lại VM.
  echo "== Docker vẫn chưa có — chạy lại startup script (VM có thể đã boot trước khi có NAT) =="
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap \
    --command="sudo google_metadata_script_runner startup" || true
  for i in $(seq 1 30); do
    vm_has_docker && break
    sleep 10
  done
fi

vm_has_docker || {
  echo "VM $VM_NAME vẫn không có Docker sau khi chạy lại startup script." >&2
  echo "Xem log trên VM: gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap \\" >&2
  echo "  --command='sudo journalctl -u google-startup-scripts --no-pager | tail -50'" >&2
  exit 1
}

echo "== pushing the compose file, starting qdrant =="
gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap \
  --command="sudo mkdir -p /opt/app && sudo chown -R \$(whoami) /opt/app"
gcloud compute scp infra/gcp/docker-compose.internal.yml "$VM_NAME":/opt/app/docker-compose.yml \
  --zone="$ZONE" --tunnel-through-iap
gcloud compute ssh "$VM_NAME" --zone="$ZONE" --tunnel-through-iap \
  --command="cd /opt/app && sudo docker compose up -d"

VM_IP=$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --format='value(networkInterfaces[0].networkIP)')

echo "== service accounts =="
gcloud iam service-accounts describe "$RUN_SA@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$RUN_SA" --display-name="Cloud Run runtime (backend/reranker)"
gcloud iam service-accounts describe "$DEPLOY_SA@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$DEPLOY_SA" --display-name="GitHub Actions deployer"

for role in roles/cloudsql.client roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$RUN_SA@$PROJECT_ID.iam.gserviceaccount.com" --role="$role" --quiet >/dev/null
done
# logging.logWriter + storage.admin là cho Cloud Build, không phải thừa: project
# GCP tạo từ 2024 trở đi KHÔNG còn được cấp service account mặc định của Cloud
# Build (`<project-number>@cloudbuild.gserviceaccount.com`), nên `gcloud builds
# submit` phải được chỉ định --service-account tường minh — và SA đó tự lo lấy
# quyền ghi log cùng bucket staging. Thiếu bước này thì lỗi là PERMISSION_DENIED
# trần trụi, không nói SA nào thiếu quyền gì.
for role in roles/run.admin roles/artifactregistry.writer roles/cloudsql.client \
            roles/iam.serviceAccountUser roles/logging.logWriter roles/storage.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$DEPLOY_SA@$PROJECT_ID.iam.gserviceaccount.com" --role="$role" --quiet >/dev/null
done

# VM chạy bằng service account compute mặc định, và project mới không còn tự
# cấp role nào cho nó. Không có logging.logWriter thì startup script hỏng mà
# Cloud Logging trống trơn — chỉ còn journalctl trên một VM không có public IP,
# tức là mù đúng lúc cần nhìn nhất.
VM_SA="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$VM_SA" --role=roles/logging.logWriter --quiet >/dev/null

echo "== Workload Identity Federation (GitHub Actions auths without a long-lived key) =="
gcloud iam workload-identity-pools describe "$WIF_POOL" --location=global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$WIF_POOL" --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" \
  --workload-identity-pool="$WIF_POOL" --location=global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" \
    --workload-identity-pool="$WIF_POOL" --location=global \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='$GITHUB_REPO'"

PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA@$PROJECT_ID.iam.gserviceaccount.com" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$WIF_POOL/attribute.repository/$GITHUB_REPO" \
  >/dev/null

echo "== storing DATABASE_URL in Secret Manager (sync-secrets.sh handles the rest) =="
for pair in "app-database-url=$DATABASE_URL"; do
  name="${pair%%=*}"; value="${pair#*=}"
  gcloud secrets describe "$name" >/dev/null 2>&1 || gcloud secrets create "$name" --replication-policy=automatic
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=-
done

cat <<EOF

Done. Next steps:

1. Add these as GitHub Actions repo variables/secrets in $GITHUB_REPO:
     GCP_PROJECT_ID=$PROJECT_ID
     GCP_REGION=$REGION
     GCP_SQL_CONNECTION_NAME=$SQL_CONNECTION_NAME
     GCP_INTERNAL_VM_IP=$VM_IP
     GCP_WORKLOAD_IDENTITY_PROVIDER=projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$WIF_POOL/providers/$WIF_PROVIDER
     GCP_DEPLOY_SA=$DEPLOY_SA@$PROJECT_ID.iam.gserviceaccount.com
     GCP_RUN_SA=$RUN_SA@$PROJECT_ID.iam.gserviceaccount.com

2. Run the one-time pgvector step printed above against $SQL_INSTANCE.

3. Copy .env.example to infra/gcp/.env.production, fill in real API keys/passwords
   (leave DATABASE_URL/QDRANT_URL/SMTP_HOST blank — deploy.yml
   sets those from infra directly), then:
     bash infra/gcp/sync-secrets.sh

4. Push to main — .github/workflows/deploy.yml takes it from there.

5. AFTER the first successful deploy (it creates the Cloud Run services the
   schedule turns on and off), set up the 12/24 on-off schedule:
     PROJECT_ID=$PROJECT_ID bash infra/gcp/schedule.sh
   then add the GCP_SCHEDULER_URL it prints as a GitHub Actions repo variable.
   Until that runs, everything stays on 24/24 — see DEPLOYMENT.md, "Chi phí".
EOF
