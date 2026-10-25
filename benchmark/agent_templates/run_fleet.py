"""Dựng lại cả fleet benchmark từ các fixture trong `templates/`, đi hết pipeline thật.

Vì sao là một script chứ không phải một bài test: các fixture ở đây mô tả **cấu
trúc graph** mà nền tảng phải chạy được, và cách duy nhất để biết chúng còn chạy
được là đẩy chúng qua đúng những cửa mà một người tạo agent phải đi —
spec → xác nhận spec → workflow → xác nhận workflow → sandbox → eval → xin duyệt
→ duyệt → deploy. Mỗi bước là một lời gọi HTTP thật tới backend đang chạy, không
có đường tắt nào gọi thẳng vào `src/`.

Chạy: xem README.md cùng thư mục.
"""

import argparse
import json
import pathlib
import sys
import time

import httpx

BASE = "http://127.0.0.1:8065/api/v1"
HERE = pathlib.Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"

CREATOR = ("creator@example.com", "creator-dev-password")
ADMIN = ("admin@example.com", "admin-dev-password")


class Client:
    def __init__(self, email: str, password: str) -> None:
        self.c = httpx.Client(timeout=600.0)
        token = self.c.post(f"{BASE}/auth/login", json={"email": email, "password": password})
        token.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {token.json()['access_token']}"

    def j(self, method: str, path: str, **kw):
        # Một lời gọi kéo dài hàng phút (sinh bộ đề, chạy eval) thỉnh thoảng bị
        # tầng vận chuyển cắt giữa chừng trên Windows. Thử lại là an toàn ở đây
        # vì mọi endpoint bộ này gọi đều tự chống trùng: sinh bộ đề bỏ qua tiêu
        # chí đã có case, còn các bước chuyển trạng thái trả 409 chứ không lặp.
        for attempt in range(3):
            try:
                r = self.c.request(method, f"{BASE}{path}", **kw)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise
                print(f"    (thử lại {method} {path}: {exc})", flush=True)
                time.sleep(3)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:1500]}")
            return r.json() if r.content else None
        return None

    def sse(self, path: str, payload: dict) -> list[tuple[str, object]]:
        frames: list[tuple[str, object]] = []
        with self.c.stream("POST", f"{BASE}{path}", json=payload) as r:
            if r.status_code >= 400:
                body = b"".join(r.iter_bytes()).decode("utf-8", "replace")
                raise RuntimeError(f"POST {path} -> {r.status_code}: {body[:1500]}")
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        frames.append((event, json.loads(raw)))
                    except json.JSONDecodeError:
                        frames.append((event, raw))
        return frames


def load(path: pathlib.Path, kb_id: int) -> dict:
    return json.loads(path.read_text(encoding="utf-8").replace("{{KB_ID}}", str(kb_id)))


def ensure_knowledge_base(creator: Client) -> int:
    """Kệ tài liệu mà fixture 02 trỏ vào — tạo nếu chưa có, nạp lại nếu chưa index."""
    name = "Benchmark — Quy chế biên bản họp"
    for kb in creator.j("GET", "/knowledge-bases"):
        if kb["name"] == name:
            return kb["id"]
    kb = creator.j(
        "POST",
        "/knowledge-bases",
        json={"name": name, "description": "Tài liệu cố định của bộ benchmark agent template"},
    )
    doc_path = HERE / "knowledge" / "quy-che-bien-ban-hop.md"
    creator.j(
        "POST",
        f"/knowledge-bases/{kb['id']}/documents",
        files={"file": (doc_path.name, doc_path.read_bytes(), "text/markdown")},
    )
    return kb["id"]


def interview_to_version(creator: Client, fixture: dict) -> tuple[int, int]:
    """Phỏng vấn thật rồi dựng version — bước duy nhất phải nói chuyện với model."""
    spec = fixture["spec"]
    description = (
        f"{spec['goal']}. Người dùng: {'; '.join(spec['users'])}. "
        f"Dữ liệu: {'; '.join(d['name'] for d in spec['data'])}. "
        f"Agent phải làm được: {'; '.join(spec['actions'])}. "
        f"Chạy xong người dùng nhận được: {'; '.join(spec['expected_outcome'])}. "
        f"Thế nào là làm đúng: {'; '.join(c['text'] for c in spec['success_criteria'])}. "
        f"Tuyệt đối không được: {'; '.join(spec['prohibitions'])}."
    )
    session = creator.j("POST", "/solution-interviews", json={"description": description})
    for _ in range(8):
        if session["complete"]:
            break
        session = creator.j(
            "POST",
            f"/solution-interviews/{session['id']}/answers",
            json={"answer": description},
        )
    if not session["complete"]:
        raise RuntimeError(f"Phỏng vấn không hoàn tất: còn thiếu {session['remaining']} trường")
    proposed = creator.j("POST", f"/solution-interviews/{session['id']}/spec")
    return proposed["agent_id"], proposed["version_id"]


def confirmed_spec(creator: Client, agent_id: int, version_id: int, fixture: dict) -> None:
    """Thay đúng bảy trường bằng bản trong fixture, rồi xác nhận."""
    creator.j("PUT", f"/agents/{agent_id}/versions/{version_id}/solution-spec", json=fixture["spec"])
    creator.j("POST", f"/agents/{agent_id}/versions/{version_id}/solution-spec/confirmation")


def confirmed_workflow(creator: Client, agent_id: int, version_id: int, fixture: dict) -> int:
    """Dựng một bản nháp từ mẫu (rẻ hơn soạn), thay bằng graph trong fixture, rồi xác nhận.

    `PUT .../workflow` chính là cái nút Lưu của canvas, nên đây không phải cửa
    sau: đó là đúng đường mà một người tạo agent sửa graph nền tảng vừa dựng cho.
    """
    creator.j(
        "POST",
        f"/agents/{agent_id}/versions/{version_id}/workflow",
        json={"template_id": "direct_assistant"},
    )
    saved = creator.j(
        "PUT", f"/agents/{agent_id}/versions/{version_id}/workflow", json=fixture["workflow"]
    )
    version_id = saved["version_id"]
    problems = saved.get("problems") or []
    if problems:
        raise RuntimeError(f"Graph còn lỗi: {problems}")
    creator.j("POST", f"/agents/{agent_id}/versions/{version_id}/workflow/confirm")
    return version_id


def sandbox(creator: Client, agent_id: int, version_id: int, message: str, decide: bool | None):
    """Một lượt chạy thử thật, và quyết định cái approval mà **chính lượt này** mở ra.

    Chụp danh sách approval đang treo TRƯỚC khi chạy rồi chỉ quyết cái mới xuất
    hiện. Lấy `pending[0]` thì sai: một lượt chạy trước đó dừng ở cổng duyệt mà
    chưa ai quyết vẫn còn nằm trong danh sách, và quyết định của lượt này sẽ
    rơi vào lượt cũ — hai lượt chạy đều có thật, nhưng bảng bằng chứng lại ghép
    quyết định vào sai lượt.
    """
    approvals = f"/agents/{agent_id}/versions/{version_id}/workflow-approvals"
    before = {row["approval_id"] for row in creator.j("GET", approvals)}
    frames = creator.sse(
        f"/agents/{agent_id}/versions/{version_id}/workflow-sandbox-runs",
        {"message": message, "conversation_id": None},
    )
    if decide is None:
        return frames
    opened = [row for row in creator.j("GET", approvals) if row["approval_id"] not in before]
    for row in opened:
        frames += creator.sse(
            f"{approvals}/{row['approval_id']}",
            {"approved": decide, "reason": "" if decide else "Chưa đúng quy trình, làm lại"},
        )
    return frames


def _matches(case: dict, when: dict) -> bool:
    """Có phải case này là cái mà một mục `eval_corrections` đang nói tới không.

    Khớp theo *hình dạng* (loại luật, action, category, quyết định) chứ không
    theo id, vì id của case sinh ra mới mỗi lần chạy lại, còn hình dạng thì
    không — đó là cái làm cho phần sửa bộ đề trong fixture chạy lại được.
    """
    check = case.get("expected_check") or {}
    if "check_kind" in when and check.get("kind") != when["check_kind"]:
        return False
    if "action" in when and when["action"] not in (
        [check.get("action")] if check.get("action") else []
    ) + list(check.get("forbidden_after") or []) + list(check.get("required_after") or []):
        return False
    if "category" in when and case.get("category") != when["category"]:
        return False
    if "decision" in when and check.get("decision") != when["decision"]:
        return False
    if "criterion_contains" in when and when["criterion_contains"] not in (case.get("criterion") or ""):
        return False
    return True


def apply_corrections(creator: Client, agent_id: int, version_id: int, fixture: dict) -> None:
    """Những chỗ bộ sinh đề viết sai mà người tạo phải sửa tay — ghi sẵn trong fixture.

    Mỗi mục kèm `why`, vì một correction không nói lý do thì lần sau không ai
    dám bỏ nó đi kể cả khi nền tảng đã sửa xong cái lỗi sinh ra nó.
    """
    corrections = fixture.get("eval_corrections") or []
    if not corrections:
        return
    base = f"/agents/{agent_id}/versions/{version_id}/eval-cases"
    cases = creator.j("GET", base)
    for rule in corrections:
        if rule["do"] == "add":
            creator.j(
                "POST",
                base,
                json={k: rule[k] for k in ("criterion", "input_message", "expected")},
            )
            print(f"    + case tay: {rule['why']}", flush=True)
            continue
        for case in cases:
            if not _matches(case, rule["when"]):
                continue
            if rule["do"] == "delete":
                creator.j("DELETE", f"{base}/{case['id']}")
                print(f"    - bỏ case {case['id']}: {rule['why']}", flush=True)
            elif rule["do"] == "set_input":
                creator.j("PATCH", f"{base}/{case['id']}", json={"input_message": rule["input_message"]})
                print(f"    ~ sửa đầu vào case {case['id']}: {rule['why']}", flush=True)


def run_one(creator: Client, admin: Client, path: pathlib.Path, kb_id: int) -> dict:
    fixture = load(path, kb_id)
    print(f"\n=== {fixture['id']} ===", flush=True)

    agent_id, version_id = interview_to_version(creator, fixture)
    creator.j("PATCH", f"/agents/{agent_id}", json={"name": fixture["name"]})
    print(f"  agent {agent_id}, version {version_id}", flush=True)

    confirmed_spec(creator, agent_id, version_id, fixture)
    version_id = confirmed_workflow(creator, agent_id, version_id, fixture)
    print(f"  workflow confirmed on version {version_id}", flush=True)

    inputs = fixture["sandbox_inputs"]
    has_approval = any(n["type"] == "human_approval" for n in fixture["workflow"]["nodes"])
    if "asks_back" in inputs:
        # Cũng phải quyết định: trên graph mà node duyệt đứng TRƯỚC node collect,
        # một đầu vào thiếu thông tin vẫn dừng ở cổng duyệt chứ không dừng ở
        # collect, và bỏ nó treo ở đó là để lại một approval chờ mãi.
        sandbox(creator, agent_id, version_id, inputs["asks_back"], False if has_approval else None)
    sandbox(creator, agent_id, version_id, inputs["full"], True if has_approval else None)
    if has_approval:
        sandbox(creator, agent_id, version_id, inputs["full"], False)
    print("  sandbox runs done", flush=True)

    generated = creator.j("POST", f"/agents/{agent_id}/versions/{version_id}/workflow-eval-cases/generate")
    print(f"  eval cases: {len(generated['cases'])}, skipped {generated['skipped_criteria']}", flush=True)
    apply_corrections(creator, agent_id, version_id, fixture)
    run = creator.j("POST", f"/agents/{agent_id}/versions/{version_id}/workflow-eval-runs")
    print(f"  eval {run['passed_count']}/{run['total_count']} = {run['pass_rate']:.2f}", flush=True)
    for result in run["results"]:
        if not result.get("passed"):
            print(f"    trượt case {result.get('case_id')}: {(result.get('reason') or '')[:140]}", flush=True)
    if not run["meets_threshold"]:
        # Dừng ở đây thay vì đâm vào 409 của cửa xin duyệt. Đọc lý do trượt ở
        # trên trước: bộ đề do model sinh ra thường viết đầu vào KHÔNG mang đủ
        # tham số bắt buộc (một địa chỉ có '@' cho `to`, một `title`), nên lượt
        # chạy dừng ở node collect và không bao giờ tới được nhánh đang bị chấm.
        # Sửa `input_message` của case bằng PATCH .../eval-cases/{id} rồi chạy lại.
        print("  DỪNG: eval dưới ngưỡng, chưa xin duyệt được", flush=True)
        return {
            "template": fixture["id"],
            "agent_id": agent_id,
            "version_id": version_id,
            "eval_pass_rate": run["pass_rate"],
            "eval_counts": [run["passed_count"], run["total_count"]],
            "deployed": False,
        }

    creator.j("POST", f"/agents/{agent_id}/versions/{version_id}/approval-request")
    admin.j("POST", f"/admin/approvals/{version_id}/approve")
    deployed = creator.j("PUT", f"/agents/{agent_id}/deployed-version", json={"version_id": version_id})
    print(f"  deployed: {deployed['status']}", flush=True)

    return {
        "template": fixture["id"],
        "agent_id": agent_id,
        "version_id": version_id,
        "eval_pass_rate": run["pass_rate"],
        "eval_counts": [run["passed_count"], run["total_count"]],
        "deployed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="chỉ chạy fixture có id này")
    args = parser.parse_args()

    creator = Client(*CREATOR)
    admin = Client(*ADMIN)
    kb_id = ensure_knowledge_base(creator)
    print(f"knowledge base {kb_id}", flush=True)

    results = []
    for path in sorted(TEMPLATES.glob("*.json")):
        fixture_id = json.loads(path.read_text(encoding="utf-8").replace("{{KB_ID}}", "0"))["id"]
        if args.only and args.only != fixture_id:
            continue
        results.append(run_one(creator, admin, path, kb_id))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
