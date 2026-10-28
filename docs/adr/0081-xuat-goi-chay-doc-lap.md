---
status: accepted
---

# Nút "Xuất gói chạy độc lập"

DS-13 tới DS-18 dựng runtime của gói và các phần nó cần. DS-19 cho người tạo một nút trên tab Deploy để tải về đúng một gói zip, công ty giải nén là chạy được.

## Quyết định

1. **Chỉ version đã duyệt (`approved`).**
   - Thứ rời nền tảng để chạy không người trông trong một công ty phải là thứ admin đã duyệt.
   - Nút bị khoá kèm lời giải thích, và route `GET .../package` trả 409 cho version chưa duyệt.
2. **Zip gồm:**
   - `bundle.json`: kèm `database_schema` (DS-15).
   - `runtime.yaml`: sinh riêng cho agent này. Có Hệ CSDL của nó, bảng ánh xạ model nó dùng, khối `mail` nếu agent gửi mail, khối `documents`/`embedding`/`qdrant`/`reranker` nếu agent tìm trong tài liệu. Mọi giá trị công ty phải đổi đều ghi `ĐỔI`. Có cảnh báo rằng dữ liệu đi tới `llm.base_url` và `embedding.base_url`. File load được ngay như đã ghi.
   - `access.yaml`: quy tắc truy cập của từng Hệ CSDL; có nhiều Hệ CSDL thì mỗi cái một file `access-<id>.yaml`.
   - `documents/`: tài liệu và bản trích xuất (DS-18).
   - `docker-compose.yml`: chép từ `deploy/package/`.
   - `.env.example`: chỉ có tên các secret.
   - `README.md`: các bước điền secret, nối SSO, trỏ LLM nội bộ; lưu ý MySQL chỉ có khi có Hệ CSDL MySQL.
3. **Không secret nào vào zip, theo cấu trúc chứ không nhờ lọc.**
   - Module xuất gói không đọc secret store. DSN chỉ xuất hiện dưới dạng tên secret (`dsn_<id>`) trong `runtime.yaml` và một dòng `AGENT_SECRET_DSN_<id>=` để trống trong `.env.example`.
   - Test quét toàn bộ zip để chắc DSN và mật khẩu trong secret store không có trong đó.
   - Hệ CSDL chưa có quy tắc truy cập, kho tài liệu không mang theo được, hay Hệ CSDL không thuộc chủ agent: mỗi trường hợp là một 409 nêu tên, trước khi ghi byte nào.
4. **Air-gap dùng lại cách đóng gói của UC1-34.**
   - `scripts/package_runtime_images.sh` chạy trên máy build có mạng: build `agent-runtime` và `agent-reranker`, pull Postgres/Qdrant, `docker save`, rồi ký bằng cosign hoặc minisign.
   - Admin đặt `PACKAGE_IMAGES_PATH` trỏ tới file đó. Khi đó tuỳ chọn "Kèm images cho air-gap" mới bật được, và zip có thêm `images/` (file tar lưu không nén, chữ ký, `load_images.sh` kiểm chữ ký trước rồi mới `docker load`).
   - Chưa có bộ images thì tuỳ chọn bị khoá kèm lời hướng dẫn.
   - Nền tảng không tự `docker save` trong một request: backend không có Docker, và việc ký là quy trình phát hành.
5. **Mặc định an toàn cho `trusted_header`.** Compose của gói chỉ mở cổng trên `127.0.0.1` (`AGENT_BIND`), vì `trusted_header` chỉ an toàn khi có gateway đứng trước. README hướng dẫn chuyển sang `oidc`.
6. **Smoke test trên CI.**
   - `scripts/smoke_package.sh` (job `package-smoke`) ghép một gói bằng `assemble_package`, tức đúng phần thuần của bước xuất.
   - Script giải nén, điền mọi secret mà `.env.example` nêu, chạy `docker compose up`, rồi kiểm `/health`, trang chat, `/chat` trả 401 khi không có người hỏi, và mở được lượt chạy khi có người hỏi.

## Consequences

- Gói không kèm images thì công ty phải lấy image `agent-runtime` từ registry của nhà cung cấp (`AGENT_RUNTIME_IMAGE`).
- Smoke test không có LLM thật: nó kiểm gói khởi động và mở được lượt chạy, không kiểm chất lượng câu trả lời.
- Đổi tên service hay image trong `deploy/package/docker-compose.yml` phải đổi cùng `scripts/package_runtime_images.sh`.
