---
status: accepted
---

# Gỡ pipeline UC-1 song song

ADR-0059 §11: các năng lực của UC-1 đã có mặt trong V2.
- Hệ CSDL có quy tắc truy cập (DS-02/03).
- Node `data_query` (DS-04).
- `action_hash` (DS-08).
- Audit chain (DS-10).
- Meta-agent sinh `data_query` (DS-11).

Pipeline UC-1 song song (`AgentSpec` → compiler LangGraph → PEP qua OPA) giờ chỉ còn là một con đường thứ hai làm cùng việc. DS-21 gỡ nó.

## Quyết định

1. **Gỡ code.**
   - Backend: `src/agentspec/`, `src/compiler/`, `src/policy/`; các route `uc1_*`, `connections`, sandbox UC-1, `metaagent/draft-spec`; `src/workflow/uc1_*`, `src/evals/uc1_*`, `src/services/uc1_*`, `postgres_introspection`; `metaagent/draft_spec.py`. Phần `metaagent/loop.py` và `catalog.py` thuộc V2 nên ở lại.
   - Frontend: các trang `UC1*.jsx` và `MetaAgentDraft.jsx`; các view "Enterprise" trong `App.jsx`; các nhóm endpoint UC-1 trong `api.js`.
   - Scripts: `import_golden_set.py`, `run_ac_checklist.py`, `seed_uc1_credentials.py` (kèm job deploy và target `make` của nó).
   - Không còn import nào tới module đã gỡ; một vòng import toàn bộ `src/` không gãy module nào.
2. **Gỡ OPA sidecar** khỏi `docker-compose.yml`, `docker-compose.airgap.yml` (cùng image của nó trong bộ air-gap), VM GCP (compose, firewall 8181, việc đẩy `authz.rego`) và CI (job `opa test`). Gỡ cấu hình `opa_*` và `network_policy_*`; gỡ provision/suspend role Postgres theo từng agent (nằm trong `src/agentspec/roles.py` đã gỡ).
3. **Migration `a7d2c4e9f1b3`** chạy được trên DB còn dữ liệu UC-1:
   - Xoá các bảng `uc1_*` cùng kiểu enum của chúng. Dữ liệu `uc1_connections` đã được chuyển sang `database_sources` từ DS-02.
   - Xoá các bộ eval gắn với agent UC-1 (`eval_suites.uc1_agent_id`); case và lượt chạy của chúng bị xoá theo qua cascade. Sau đó bỏ hẳn cột này.
   - Đổi các tài khoản `sales_manager`/`sales_rep` thành `creator`, rồi dựng lại kiểu enum `userrole` không còn hai nhãn đó. Ai được xem dòng nào giờ do quy tắc truy cập của Hệ CSDL quyết định, không phải vai trò nền tảng.
   - Migration một chiều: downgrade báo lỗi và chỉ cách khôi phục từ bản sao lưu.
   - Migration cũ `3f9c7a2e6b18` (seed hai tài khoản sales) được đóng băng dữ liệu ngay trong file, để `alembic upgrade head` trên DB mới không phụ thuộc vào code app đã gỡ.
4. **Giữ những gì không phải pipeline.**
   - `agent_runs` có `pipeline='uc1'` vẫn đọc được: giữ `RunPipeline.UC1` cho lịch sử của PM.
   - Bề mặt dữ liệu ERP demo của `schema.sql` (`fin_invoice`, `v_ar_open`…) cùng `seed_uc1_demo_data.py`/`verify_uc1_data_surface.py` vẫn giữ.
   - `AGENT_ADMIN` vẫn là vai trò tương đương admin.
   - Control plane (`pull_template`) vẫn giữ, và một test khẳng định chỉ route admin import nó.

## Consequences

- `docs/usecase_spec/` và `docs/uc1-demo-runbook.md` là tài liệu lịch sử, có ghi chú ở đầu. Demo hiện hành là của V2 (DS-24).
- Deployment cũ cần dọn một lần: container `policy` trên VM, firewall 8181, job `backend-seed-credentials`. Các bước nằm trong `DEPLOYMENT.md`.
