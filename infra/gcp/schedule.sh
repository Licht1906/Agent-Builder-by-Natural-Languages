#!/usr/bin/env bash
set -euo pipefail

# Dựng lịch bật/tắt hạ tầng: một Cloud Function (scheduler/main.py) cộng hai
# Cloud Scheduler job gọi nó. Chạy một lần, sau infra/gcp/setup.sh. Idempotent.
#
# Vì sao là function chứ không phải Scheduler gọi thẳng REST API của Google:
# bật lên không phải một lời gọi mà là một chuỗi có thứ tự và có chờ — SQL
# phải RUNNABLE trước khi Cloud Run lên. Chuỗi đó cần một chỗ để sống, và
# cần log đọc được khi nó hỏng.
#
# Usage: PROJECT_ID=xxx bash infra/gcp/schedule.sh

cd "$(dirname "$0")/../.."

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-southeast1}"
ZONE="${ZONE:-${REGION}-a}"

# Khung giờ service available. Đổi ở đây rồi chạy lại script là cả function
# lẫn hai scheduler job cùng đổi theo — deploy.yml hỏi function chứ không tự
# giữ bản sao của khung giờ.
WINDOW_TZ="${WINDOW_TZ:-Asia/Ho_Chi_Minh}"
WINDOW_START_HOUR="${WINDOW_START_HOUR:-8}"
WINDOW_END_HOUR="${WINDOW_END_HOUR:-20}"
WINDOW_DAYS="${WINDOW_DAYS:-0,1,2,3,4,5,6}"   # 0=T2 … 6=CN, theo Python weekday()

FN_NAME=app-scheduler
FN_SA=app-scheduler-sa
SCHED_SA=app-scheduler-invoker
SQL_INSTANCE=app-db
VM_NAME=internal-services

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== bật API =="
gcloud services enable \
  cloudfunctions.googleapis.com cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com run.googleapis.com eventarc.googleapis.com

echo "== service account cho function =="
gcloud iam service-accounts describe "$FN_SA@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$FN_SA" --display-name="Bật/tắt hạ tầng theo lịch"

# cloudsql.admin: đổi activationPolicy là sửa settings của instance, .client không đủ.
# run.admin: để đổi min-instances.
# serviceAccountUser: revision mới mà PATCH tạo ra chạy dưới danh nghĩa app-run-sa.
# artifactregistry.reader: đổi min-instances là PATCH service, mà PATCH tạo một
#   revision mới — nên Cloud Run bắt CHÍNH NGƯỜI GỌI phải đọc được image đang
#   dùng. Thiếu nó thì lỗi là 403 'artifactregistry.repositories.downloadArtifacts
#   denied', nghe như vấn đề kéo image lúc chạy chứ không như thiếu quyền lúc gọi.
for role in roles/cloudsql.admin roles/compute.instanceAdmin.v1 roles/run.admin \
            roles/iam.serviceAccountUser roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$FN_SA@$PROJECT_ID.iam.gserviceaccount.com" --role="$role" --quiet >/dev/null
done

echo "== service account cho Cloud Scheduler =="
gcloud iam service-accounts describe "$SCHED_SA@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SCHED_SA" --display-name="Cloud Scheduler gọi app-scheduler"

echo "== deploy function =="
# timeout 540s: bật lên gồm chờ Cloud SQL RUNNABLE (~1–3 phút) rồi chờ VM.
# `^@^` đổi dấu phân cách của --set-env-vars từ dấu phẩy sang @: RUN_SERVICES
# và WINDOW_DAYS đều chứa dấu phẩy, để mặc định thì gcloud cắt chúng thành
# nhiều biến rác và function chết ngay ở dòng import.
# --build-service-account: function gen2 build qua Cloud Build, và project GCP
# tạo từ 2024 trở đi không còn được cấp service account mặc định của Cloud
# Build. Thiếu cờ này thì deploy chết bằng PERMISSION_DENIED không nói SA nào.
# Dùng lại SA của deployer, vốn đã có logging.logWriter + storage.admin.
gcloud functions deploy "$FN_NAME" \
  --gen2 --region="$REGION" --runtime=python312 \
  --source=infra/gcp/scheduler --entry-point=handle \
  --build-service-account="projects/$PROJECT_ID/serviceAccounts/${GCP_DEPLOY_SA:-gh-actions-deployer@$PROJECT_ID.iam.gserviceaccount.com}" \
  --trigger-http --no-allow-unauthenticated \
  --timeout=540s --memory=512Mi \
  --service-account="$FN_SA@$PROJECT_ID.iam.gserviceaccount.com" \
  --set-env-vars="^@^PROJECT_ID=$PROJECT_ID@REGION=$REGION@ZONE=$ZONE@SQL_INSTANCE=$SQL_INSTANCE@VM_NAME=$VM_NAME@RUN_SERVICES=backend,reranker@WINDOW_TZ=$WINDOW_TZ@WINDOW_START_HOUR=$WINDOW_START_HOUR@WINDOW_END_HOUR=$WINDOW_END_HOUR@WINDOW_DAYS=$WINDOW_DAYS"

FN_URL=$(gcloud functions describe "$FN_NAME" --region="$REGION" --gen2 --format='value(serviceConfig.uri)')

# Function gen2 là một Cloud Run service, nên quyền gọi là roles/run.invoker.
# Hai caller: Cloud Scheduler (hai job dưới đây) và GitHub Actions — deploy.yml
# gọi action=start trước khi migrate và action=reconcile sau khi deploy xong.
echo "== cấp quyền gọi function =="
DEPLOY_SA_EMAIL="${GCP_DEPLOY_SA:-gh-actions-deployer@$PROJECT_ID.iam.gserviceaccount.com}"
for member in \
  "serviceAccount:$SCHED_SA@$PROJECT_ID.iam.gserviceaccount.com" \
  "serviceAccount:$DEPLOY_SA_EMAIL"; do
  gcloud run services add-iam-policy-binding "$FN_NAME" \
    --region="$REGION" --role=roles/run.invoker \
    --member="$member" --quiet >/dev/null
  echo "  run.invoker -> $member"
done

echo "== hai scheduler job =="
upsert_job() {
  local name="$1" schedule="$2" action="$3"
  local verb=create
  gcloud scheduler jobs describe "$name" --location="$REGION" >/dev/null 2>&1 && verb=update
  gcloud scheduler jobs "$verb" http "$name" \
    --location="$REGION" \
    --schedule="$schedule" --time-zone="$WINDOW_TZ" \
    --uri="$FN_URL?action=$action" --http-method=POST \
    --oidc-service-account-email="$SCHED_SA@$PROJECT_ID.iam.gserviceaccount.com" \
    --oidc-token-audience="$FN_URL" \
    --attempt-deadline=540s \
    --quiet >/dev/null
  echo "  $name ($verb): $schedule $WINDOW_TZ -> action=$action"
}

upsert_job app-start "0 $WINDOW_START_HOUR * * *" start
upsert_job app-stop  "0 $WINDOW_END_HOUR * * *"  stop

cat <<MSG

Xong. Hạ tầng sẽ tự bật lúc ${WINDOW_START_HOUR}:00 và tắt lúc ${WINDOW_END_HOUR}:00 ($WINDOW_TZ).

Thêm biến này vào GitHub Actions repo variables (deploy.yml cần nó để bật hạ
tầng lên trước khi migrate, rồi trả lại trạng thái cũ sau khi deploy xong):

  GCP_SCHEDULER_URL=$FN_URL

Kiểm tra ngay, đừng đợi 20:00 mới biết nó hỏng:

  gcloud scheduler jobs run app-stop  --location=$REGION
  gcloud functions logs read $FN_NAME --region=$REGION --gen2 --limit=50
  gcloud scheduler jobs run app-start --location=$REGION

MSG
