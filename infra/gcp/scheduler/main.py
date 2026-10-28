"""Bật/tắt hạ tầng Agent Builder by Natural Languages theo giờ, để không trả tiền cho 12 tiếng không ai dùng.

Cloud Scheduler gọi hàm này hai lần một ngày (xem ../schedule.sh). Ba action:

  start      bật tất cả, bất kể bây giờ là mấy giờ
  stop       tắt tất cả, bất kể bây giờ là mấy giờ
  reconcile  đưa về đúng trạng thái *lẽ ra* phải có ở thời điểm hiện tại

`reconcile` tồn tại vì deploy.yml cần bật hạ tầng lên giữa đêm để chạy
`alembic upgrade head`, rồi phải trả lại trạng thái cũ. Nó hỏi cùng một
window config như hai scheduler job, nên logic "giờ làm việc là mấy giờ" chỉ
nằm ở một chỗ duy nhất — sửa env var của function là cả deploy lẫn scheduler
đổi theo.

Thứ tự bật/tắt không đối xứng, và đó là chủ ý:

  bật:  Cloud SQL + VM trước (chờ SQL RUNNABLE) → Cloud Run
  tắt:  Cloud Run trước → rồi Cloud SQL + VM

Bật Cloud Run trước khi DB sẵn sàng thì instance min-instance đầu tiên khởi
động vào một Cloud SQL đang STOPPED và cache connection lỗi; tắt Cloud Run sau
DB thì nó phục vụ request bằng một DB đã biến mất.

Từng có một bước re-seed Vault giữa hai bước bật, vì Vault dev-mode mất sạch
secret mỗi khi VM tắt. Từ DS-20 mọi credential nằm trong SecretStore của nền
tảng — một bảng trong Cloud SQL — nên không còn gì để seed lại mỗi sáng.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from zoneinfo import ZoneInfo

import functions_framework
import google.auth
from google.auth.transport.requests import AuthorizedSession

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROJECT_ID = os.environ["PROJECT_ID"]
REGION = os.environ["REGION"]
ZONE = os.environ.get("ZONE", f"{REGION}-a")
SQL_INSTANCE = os.environ.get("SQL_INSTANCE", "app-db")
VM_NAME = os.environ.get("VM_NAME", "internal-services")
RUN_SERVICES = [s for s in os.environ.get("RUN_SERVICES", "backend,reranker").split(",") if s]

# min-instances lúc "bật". backend chỉ cần 1 để tránh cold start cho người
# dùng; reranker giữ 1 vì model 2.2 GB tải lúc start (lý do docker-compose.yml
# và DEPLOYMENT.md đều pin nó warm).
MIN_INSTANCES_ON = int(os.environ.get("MIN_INSTANCES_ON", "1"))

WINDOW_TZ = ZoneInfo(os.environ.get("WINDOW_TZ", "Asia/Ho_Chi_Minh"))
WINDOW_START_HOUR = int(os.environ.get("WINDOW_START_HOUR", "8"))
WINDOW_END_HOUR = int(os.environ.get("WINDOW_END_HOUR", "20"))
# Thứ trong tuần được phép bật, theo Python weekday(): 0=T2 ... 6=CN.
WINDOW_DAYS = {int(d) for d in os.environ.get("WINDOW_DAYS", "0,1,2,3,4,5,6").split(",") if d != ""}

# Mỗi lần đổi min-instances là một revision Cloud Run mới. Hai lần một ngày là
# ~730 revision/năm, và quota là 1000 revision mỗi service — nên phải dọn.
KEEP_REVISIONS = int(os.environ.get("KEEP_REVISIONS", "5"))

SQL_API = "https://sqladmin.googleapis.com/v1"
COMPUTE_API = "https://compute.googleapis.com/compute/v1"
RUN_API = "https://run.googleapis.com/v2"

_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
session = AuthorizedSession(_creds)


def _call(method: str, url: str, **kw):
    r = session.request(method, url, timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {url} -> {r.status_code} {r.text[:500]}")
    return r.json() if r.content else {}


# --- Cloud SQL ------------------------------------------------------------
# Không có "stop instance" API riêng: dừng một instance Postgres là đặt
# activationPolicy=NEVER. Storage và backup vẫn tính tiền khi dừng, chỉ tiền
# vCPU/RAM là hết — đó mới là phần lớn hoá đơn.


def sql_set(running: bool) -> None:
    policy = "ALWAYS" if running else "NEVER"
    log.info("cloud sql %s -> activationPolicy=%s", SQL_INSTANCE, policy)
    _call(
        "PATCH",
        f"{SQL_API}/projects/{PROJECT_ID}/instances/{SQL_INSTANCE}",
        json={"settings": {"activationPolicy": policy}},
    )


def sql_wait_runnable(timeout_s: int = 420) -> None:
    """Chờ tới khi Postgres thực sự nhận connection được.

    Cloud SQL trả 200 cho PATCH ngay lập tức rồi mới khởi động thật, mất
    ~1–3 phút. Trả về sớm ở đây thì Cloud Run bật lên và đâm vào một DB chưa
    sẵn sàng, đúng thứ thứ tự bật/tắt đang cố tránh.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = _call("GET", f"{SQL_API}/projects/{PROJECT_ID}/instances/{SQL_INSTANCE}")
        if info.get("state") == "RUNNABLE":
            log.info("cloud sql %s RUNNABLE", SQL_INSTANCE)
            return
        log.info("cloud sql %s state=%s, chờ tiếp", SQL_INSTANCE, info.get("state"))
        time.sleep(10)
    raise TimeoutError(f"cloud sql {SQL_INSTANCE} không RUNNABLE sau {timeout_s}s")


# --- Compute Engine VM ----------------------------------------------------
# vm-startup.sh chạy lại mỗi lần boot và kết thúc bằng `docker compose up -d`,
# nên qdrant/policy tự lên lại — không cần SSH vào làm gì thêm.


def vm_set(running: bool) -> None:
    verb = "start" if running else "stop"
    log.info("vm %s -> %s", VM_NAME, verb)
    _call("POST", f"{COMPUTE_API}/projects/{PROJECT_ID}/zones/{ZONE}/instances/{VM_NAME}/{verb}")


def vm_wait_running(timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = _call("GET", f"{COMPUTE_API}/projects/{PROJECT_ID}/zones/{ZONE}/instances/{VM_NAME}")
        if info.get("status") == "RUNNING":
            log.info("vm %s RUNNING", VM_NAME)
            return
        log.info("vm %s status=%s, chờ tiếp", VM_NAME, info.get("status"))
        time.sleep(5)
    raise TimeoutError(f"vm {VM_NAME} không RUNNING sau {timeout_s}s")


# --- Cloud Run ------------------------------------------------------------


def _run_service_url(name: str) -> str:
    return f"{RUN_API}/projects/{PROJECT_ID}/locations/{REGION}/services/{name}"


def run_set_min_instances(name: str, count: int) -> None:
    """Đọc service rồi ghi lại với min-instances mới.

    Read-modify-write cả object thay vì PATCH kèm updateMask cho một field
    lồng sâu: updateMask trên `template.scaling.minInstanceCount` không phải
    lúc nào cũng được honour, còn ghi lại nguyên object thì đúng ở mọi phiên
    bản API. Giá phải trả là một revision mới mỗi lần — `run_gc_revisions`
    dọn phần đó.
    """
    svc = _call("GET", _run_service_url(name))
    template = svc.setdefault("template", {})
    scaling = template.setdefault("scaling", {})
    if scaling.get("minInstanceCount", 0) == count:
        log.info("cloud run %s đã ở min-instances=%s, bỏ qua", name, count)
        return
    scaling["minInstanceCount"] = count

    # Cloud Run sinh tên revision; giữ lại tên cũ thì PATCH đụng conflict.
    template.pop("revision", None)

    log.info("cloud run %s -> min-instances=%s", name, count)
    _call("PATCH", _run_service_url(name), json=svc)


def run_gc_revisions(name: str) -> None:
    """Xoá revision cũ, chỉ những cái không nhận traffic."""
    svc = _call("GET", _run_service_url(name))
    live = {t.get("revision") for t in svc.get("trafficStatuses", []) if t.get("revision")}
    live.add(svc.get("latestReadyRevision"))
    live.add(svc.get("latestCreatedRevision"))

    listing = _call("GET", f"{_run_service_url(name)}/revisions?pageSize=200")
    revisions = sorted(
        listing.get("revisions", []),
        key=lambda r: r.get("createTime", ""),
        reverse=True,
    )
    for rev in revisions[KEEP_REVISIONS:]:
        full_name = rev.get("name", "")
        if full_name in live or full_name.split("/")[-1] in {n.split("/")[-1] for n in live if n}:
            continue
        log.info("xoá revision cũ %s", full_name)
        try:
            _call("DELETE", f"{RUN_API}/{full_name}")
        except RuntimeError as exc:  # dọn dẹp không được phép làm hỏng lịch
            log.warning("không xoá được %s: %s", full_name, exc)


# --- điều phối ------------------------------------------------------------


def in_window(now: datetime.datetime | None = None) -> bool:
    now = (now or datetime.datetime.now(datetime.UTC)).astimezone(WINDOW_TZ)
    if now.weekday() not in WINDOW_DAYS:
        return False
    return WINDOW_START_HOUR <= now.hour < WINDOW_END_HOUR


def start() -> list[str]:
    done = []
    sql_set(True)
    vm_set(True)
    sql_wait_runnable()
    vm_wait_running()
    done += [f"cloud sql {SQL_INSTANCE} lên", f"vm {VM_NAME} lên"]

    for svc in RUN_SERVICES:
        run_set_min_instances(svc, MIN_INSTANCES_ON)
        run_gc_revisions(svc)
        done.append(f"cloud run {svc} min-instances={MIN_INSTANCES_ON}")
    return done


def stop() -> list[str]:
    done = []
    for svc in RUN_SERVICES:
        run_set_min_instances(svc, 0)
        run_gc_revisions(svc)
        done.append(f"cloud run {svc} min-instances=0")
    # Service vẫn tồn tại và vẫn trả lời — chỉ là scale-to-zero, không còn
    # instance nào chờ sẵn. Request ngoài giờ sẽ cold start rồi fail ở DB.
    sql_set(False)
    vm_set(False)
    done += [f"cloud sql {SQL_INSTANCE} xuống", f"vm {VM_NAME} xuống"]
    return done


@functions_framework.http
def handle(request):
    action = (request.args.get("action") or (request.get_json(silent=True) or {}).get("action") or "reconcile").lower()

    if action == "reconcile":
        action = "start" if in_window() else "stop"
        log.info("reconcile -> %s", action)

    if action == "start":
        done = start()
    elif action == "stop":
        done = stop()
    else:
        return ({"error": f"action không hợp lệ: {action}"}, 400)

    log.info("xong: %s", "; ".join(done))
    return ({"action": action, "done": done}, 200)
