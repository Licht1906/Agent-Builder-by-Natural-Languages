---
status: accepted
---

# Một `SecretStore` cho cả nền tảng, bỏ Vault khỏi deploy

ADR-0059 §9: nền tảng từng có hai kho secret. `EncryptedDbSecretStore` (ADR-0045) giữ credential BYODB, tool đã đăng ký và DSN của Hệ CSDL. Vault dev-mode (UC1-08) giữ credential kết nối của pipeline UC-1. Vault dev-mode lưu trong RAM nên mất sạch mỗi khi VM tắt. Trên GCP, VM tắt mỗi đêm, nên phải có một job re-seed mỗi sáng, và nếu job đó hỏng thì UC-1 trả rỗng mà không báo lỗi. DS-20 gom tất cả về một kho.

## Quyết định

1. **Mọi credential của nền tảng đọc qua `SecretStore`.**
   - Kết nối UC-1 (`uc1_connections`, các route sandbox, chat, duyệt và golden set) giờ ghi và đọc qua `get_secret_store`, khoá vẫn là `conn/<tên>`.
   - Chuỗi con trỏ `vault://conn/<tên>` trong spec UC-1 giữ nguyên định dạng (DS-21 sẽ gỡ cả pipeline), nhưng không còn Vault nào được gọi.
   - `resolve_credential_via` chuyển sang `src.services.secret_store` và giữ nguyên hợp đồng: secret chưa ai ghi trả về `""`, còn secret có nhưng không giải mã được (`SecretUnreadableError`) thì là sự cố và được ném ra.
2. **Bỏ service `vault`** khỏi `docker-compose.yml`, `docker-compose.airgap.yml` (cùng image trong bộ air-gap của UC1-34), và VM `internal-services` trên GCP: compose, firewall, `VAULT_TOKEN` trong Secret Manager.
   - Bỏ cấu hình `vault_url`, `vault_token` và `vault_kv_mount`. Một `.env` cũ còn các biến đó thì bị bỏ qua (`extra="ignore"`).
3. **Không còn re-seed mỗi sáng.** `SecretStore` là một bảng trong Cloud SQL nên sống qua mọi lần tắt/bật và có backup cùng DB. `app-scheduler` không còn bước re-seed. `deploy.yml` chạy job `backend-seed-credentials` (`scripts/seed_uc1_credentials.py`, idempotent) một lần mỗi lần deploy.
4. **Giữ client Vault cho gói.** `VaultKvStore` ở lại trong `src.services.vault_secret_store`, nhưng chỉ dùng cho `secrets.backend: vault` của gói xuất ra (DS-13), cho công ty có Vault riêng. Một test khẳng định app nền tảng (`src.main`) không nạp module này.

## Consequences

- Credential từng chỉ nằm trong Vault dev-mode không chuyển sang được, vì nó ở trong RAM. Credential UC-1 được ghi lại ở lần deploy kế tiếp; kết nối tạo tay thì phải nhập lại. `DEPLOYMENT.md` có các bước dọn một lần cho deployment dựng trước DS-20.
- Mất `SECRET_STORE_KEY` đồng nghĩa mất mọi secret đã lưu. Khoá này phải được giữ như chính DB; `.env.example` giờ có ghi chú về nó.
