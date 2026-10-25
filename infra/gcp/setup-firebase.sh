#!/usr/bin/env bash
set -euo pipefail

# Bật Firebase Hosting cho SPA `frontend/`. Chạy một lần, sau setup.sh.
# Idempotent.
#
# Vì sao Hosting phải reverse-proxy `/api/**` sang Cloud Run chứ không phải
# chỉ host file tĩnh: `frontend/src/lib/api.js` đặt `const BASE = "/api/v1"`,
# đường dẫn tương đối, và không có `VITE_*` nào trong bundle để trỏ đi chỗ
# khác. `vite.config.js` nói thẳng ý đồ đó — "the production build works
# behind any reverse proxy that maps /api to the backend, with no URL compiled
# into the bundle". Rewrite trong firebase.json chính là reverse proxy ấy.
#
# Hệ quả dễ chịu: trang và API dùng chung origin, nên CORS không tham gia vào
# đâu cả — CORS_ORIGINS trở thành không liên quan với đường đi này.
#
# Usage: PROJECT_ID=xxx bash infra/gcp/setup-firebase.sh

cd "$(dirname "$0")/../.."

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
DEPLOY_SA="${DEPLOY_SA:-gh-actions-deployer@$PROJECT_ID.iam.gserviceaccount.com}"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== bật API =="
gcloud services enable firebase.googleapis.com firebasehosting.googleapis.com

# Dùng REST API với access token của gcloud thay vì `firebase projects:addfirebase`.
# Firebase CLI giữ bộ token OAuth riêng, độc lập với gcloud, và nó hết hạn âm
# thầm: `firebase login:list` vẫn in ra tên tài khoản trong khi refresh token
# đã chết, nên lỗi hiện ra là 401 UNAUTHENTICATED chứ không phải "hãy đăng nhập
# lại". Một hệ xác thực ít hơn là một thứ ít hỏng hơn.
# X-Goog-User-Project là bắt buộc, không phải trang trí: access token của
# `gcloud` là user credential không gắn quota project, và firebase.googleapis.com
# từ chối nó bằng 403 SERVICE_DISABLED kèm một thông báo nói về "quota project"
# — nghe như API chưa bật, trong khi API đã bật rồi. Header này chỉ định
# project nào chịu quota cho lời gọi.
api() {
  local method="$1" url="$2"; shift 2
  curl -sS -X "$method" \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "X-Goog-User-Project: $PROJECT_ID" \
    -H "Content-Type: application/json" "$url" "$@"
}

echo "== gắn Firebase vào project GCP =="
if api GET "https://firebase.googleapis.com/v1beta1/projects/$PROJECT_ID" | grep -q '"projectId"'; then
  echo "   $PROJECT_ID đã là Firebase project"
else
  api POST "https://firebase.googleapis.com/v1beta1/projects/$PROJECT_ID:addFirebase" -d '{}' | head -20
  # addFirebase là long-running operation; chờ nó hiện ra thật.
  for i in $(seq 1 20); do
    api GET "https://firebase.googleapis.com/v1beta1/projects/$PROJECT_ID" | grep -q '"projectId"' && break
    sleep 5
  done
fi

echo "== hosting site =="
SITES=$(api GET "https://firebasehosting.googleapis.com/v1beta1/projects/$PROJECT_ID/sites")
if echo "$SITES" | grep -q '"name"'; then
  echo "$SITES" | grep -o '"defaultUrl": *"[^"]*"' | sed 's/.*: *"/   /; s/"$//'
else
  echo "   chưa có site nào, tạo site mặc định"
  api POST "https://firebasehosting.googleapis.com/v1beta1/projects/$PROJECT_ID/sites?siteId=$PROJECT_ID" -d '{}' | head -20
fi

echo "== quyền deploy cho GitHub Actions =="
# firebasehosting.admin để đẩy bản build; run.viewer để rewrite `/api/**` giải
# được service `backend` lúc deploy config.
for role in roles/firebasehosting.admin roles/run.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$DEPLOY_SA" --role="$role" --quiet >/dev/null
  echo "   $role -> $DEPLOY_SA"
done

cat <<MSG

Xong. Site mặc định: https://$PROJECT_ID.web.app

Deploy tay lần đầu (hoặc cứ push lên main, deploy.yml có job deploy-frontend):

  cd frontend && npm ci && npm run build && cd ..
  firebase deploy --only hosting --project $PROJECT_ID

Kiểm tra rewrite sống thật. Mọi endpoint /api/v1 đều cần đăng nhập nên 401 là
câu trả lời ĐÚNG — thứ cần nhìn là content-type: application/json nghĩa là
FastAPI đã trả lời, text/html nghĩa là Firebase trả index.html theo SPA
fallback và request chưa bao giờ rời khỏi Hosting.

  curl -s -o /dev/null -w '%{content_type}\n' https://$PROJECT_ID.web.app/api/v1/admin/config
MSG
