# Khai DSN cho một Hệ CSDL

Một Hệ CSDL (`Nguồn dữ liệu → Cơ sở dữ liệu`) được cắm vào bằng một DSN trỏ tới
**DB mẫu/staging cùng schema** với DB thật (ADR-0059 §2). DSN chỉ nằm trong
secret store; màn hình không bao giờ hiện lại nó, và mọi lỗi kết nối chỉ được
báo bằng một câu cố định — không bao giờ lặp lại host, user hay mật khẩu.

Hai việc nên làm trước khi dán DSN:

1. **Tạo một tài khoản chỉ đọc**, chỉ được `SELECT` trên các view agent sẽ đọc
   (xem mẫu cho từng loại CSDL bên dưới). Nền tảng tự giữ phiên ở chế độ chỉ
   đọc, nhưng quyền của tài khoản là lớp chặn cuối cùng.
2. **Host nội bộ cần quản trị viên cho phép.** Host phân giải ra địa chỉ nội bộ
   (10.x, 192.168.x, 127.0.0.1, …) bị chặn trừ khi nằm trong
   `database_source_trusted_hosts`. Lỗi "Địa chỉ này là địa chỉ nội bộ…" nghĩa là
   cần nhờ quản trị viên thêm host.

Mỗi lần kiểm tra kết nối và mỗi truy vấn đều có giới hạn 5 giây.

## PostgreSQL

Dạng URL hoặc dạng `từ khoá=giá trị` của libpq:

```
postgresql://agent_ro:mat-khau@erp-staging.example.com:5432/erp?sslmode=require
host=erp-staging.example.com port=5432 dbname=erp user=agent_ro password=mat-khau sslmode=require
```

Chỉ nhận các từ khoá: `host`, `hostaddr`, `port`, `dbname`, `user`, `password`,
`sslmode`, `application_name`, `connect_timeout` (bị thay bằng giới hạn 5 giây
của nền tảng). Từ khoá khác — `service`, `passfile`, `sslcert`, `sslkey`, … —
bị từ chối vì chúng đọc file trên máy chủ nền tảng. Không nhận unix socket và
nhiều host trong một DSN.

Tài khoản chỉ đọc mẫu:

```sql
CREATE ROLE agent_ro LOGIN PASSWORD 'mat-khau';
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON v_ar_open, v_staff TO agent_ro;
```

Thuộc tính của người hỏi được đặt vào phiên bằng `SET LOCAL app.<thuộc_tính>`
(tên viết thường), để RLS phía khách đọc được nếu muốn:
`current_setting('app.employee_code', true)`.

## SQL Server

Dạng chuỗi kết nối ADO.NET (như trong SSMS / `appsettings.json`) hoặc dạng URL:

```
Server=erp-staging.example.com,1433;Database=erp;User Id=agent_ro;Password=mat-khau;Encrypt=True
Server=erp-staging.example.com\SQLEXPRESS;Database=erp;User Id=agent_ro;Password={mat;khau}
mssql://agent_ro:mat-khau@erp-staging.example.com:1433/erp?encrypt=yes
```

| Thành phần | Các cách viết được nhận |
|---|---|
| Máy chủ | `Server`, `Data Source`, `Address`, `Addr`, `Network Address` — dạng `host`, `host,cổng`, `tcp:host,cổng` hoặc `host\TÊN_INSTANCE` |
| Cổng | trong `Server` (`host,1433`), hoặc `Port`; mặc định 1433 |
| Database | `Database`, `Initial Catalog` |
| Tài khoản | `User Id`, `UID`, `User` — **bắt buộc** |
| Mật khẩu | `Password`, `PWD` — có `;` thì bọc trong `{...}` |
| Mã hoá | `Encrypt=True/Yes/Mandatory` → bắt buộc TLS; `False/No/Optional` → vẫn mã hoá nếu máy chủ hỗ trợ |
| Khác | `Application Name`; `TrustServerCertificate`, `Connect Timeout` được nhận nhưng không đổi gì |

Bị từ chối, kèm lý do:

- **Không có `User Id`**, `Integrated Security`, `Trusted_Connection`: đăng nhập
  Windows sẽ dùng chính tài khoản của máy chủ nền tảng, không phải của bạn. Hãy
  dùng một SQL login.
- `Failover Partner`, `MultiSubnetFailover`: một host thứ hai không được kiểm tra.
- `.`, `(local)`, `(localdb)\…`, `np:`, `lpc:`: tên trỏ về chính máy chủ nền tảng.
- `Encrypt=Strict` (TDS 8) chưa được hỗ trợ.

**Instance có tên** (`host\SQLEXPRESS`) được tra cổng qua SQL Browser (UDP 1434)
trên đúng địa chỉ đã kiểm tra, chờ tối đa 2 giây. Nếu tường lửa chặn UDP 1434,
ghi thẳng cổng: `Server=host,cổng`.

Tài khoản chỉ đọc mẫu:

```sql
CREATE LOGIN agent_ro WITH PASSWORD = 'mat-khau';
USE erp;
CREATE USER agent_ro FOR LOGIN agent_ro;
GRANT SELECT ON dbo.v_ar_open TO agent_ro;
GRANT SELECT ON dbo.v_staff TO agent_ro;
```

SQL Server không có transaction chỉ đọc, nên nền tảng giữ chỉ đọc bằng cách: chỉ
gửi câu `SELECT` do chính nó biên dịch trên view, và mọi phiên chạy trong một
transaction luôn bị rollback. Nếu database bật `ALLOW_SNAPSHOT_ISOLATION`, truy
vấn đọc theo snapshot — không chặn và không bị chặn bởi giao dịch ghi.

View trong schema `dbo` hiện tên trần (`v_ar_open`); schema khác hiện kèm tên
schema (`sales.v_ar_open`).

Thuộc tính của người hỏi được đặt vào phiên bằng `sp_set_session_context`
(tên viết thường, chỉ đọc trong phiên), để RLS phía khách đọc được nếu muốn:
`SESSION_CONTEXT(N'employee_code')`.

So sánh với bảng nhân viên theo collation của database — thường không phân biệt
hoa thường, nên `AN@cty.vn` khớp `an@cty.vn`.
