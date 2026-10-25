"""Stress the real Workflow Composer across many kinds of agent, live.

    python scripts/composer_scenario_battery.py                # every scenario, once
    python scripts/composer_scenario_battery.py --repeats 3     # each scenario 3 times
    python scripts/composer_scenario_battery.py 04 09           # only scenarios matching these prefixes

**Costs real money and makes real API calls** — this is the point. A scripted
double proves the harness works, not that the prompt does; `compose_workflow`
here runs on the true production path (`document_model()`, the true validator,
the true 3-attempt retry). MCP itself is entirely mocked (`CatalogTool` objects
below — Gmail, Drive, a CRM, an ERP, Calendar, Slack), so nothing here needs a
live connection or a network round trip beyond the model call: the point is to
stress the composer, not to test MCP transport, which already has its own
suite (`tests/test_workflow/test_mcp_*.py`).

**Origin.** Run cold on 14 scenarios × 2 repeats, this found a real, systemic
composer failure: 4 of 6 rejections were the same rule — `raw_payload_as_answer`
— a composed graph ending its answer on an `mcp` node's own JSON. The fix
(`composition._narrated`, ADR-0041's composer splicing in the validator's own
suggested repair as graph surgery instead of gambling a fourth model call) is
now pinned by `tests/test_workflow/test_composer_narration_repair.py`, and this
script re-ran clean afterward: 42/42 across every scenario at 3 repeats. Re-run
this after any change to the composer prompt or to `structural_problems` — a
scripted unit test proves a specific shape stays fixed; this is what finds the
*next* shape nobody wrote a test for yet.

**No database writes.** `compose_workflow` is a pure function of a spec, a
catalog and a model factory — nothing here creates an agent, a version, or
touches Postgres. Safe to run against any environment with a working
`OPENAI_API_KEY`.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.metaagent.loop import CatalogAction, CatalogTool  # noqa: E402
from src.models.solution_spec import AgentSolutionSpecification  # noqa: E402
from src.services.llm import _build_chat_model  # noqa: E402
from src.workflow.composition import GraphCompositionError, compose_workflow  # noqa: E402

DEFAULT_ALLOWLIST = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4.1"]


def _action(name: str, desc: str, *, read_only: bool, risk: str = "low", params=None, required=()) -> CatalogAction:
    schema = {"type": "object", "properties": {p: {"type": "string"} for p in (params or [])}}
    if required:
        schema["required"] = list(required)
    return CatalogAction(name=name, description=desc, read_only=read_only, risk=risk, input_schema=schema)


#: A small, fake connection catalog spanning read-only/write/high-risk actions
#: across several distinct business domains — the shapes a composer actually
#: has to draw around, without any of them being a real MCP server.
GMAIL = CatalogTool(id="gmail", name="Gmail", actions=(
    _action("search_emails", "Tìm thư theo từ khoá", read_only=True, params=["query"]),
    _action("send_email", "Gửi email — không lấy lại được", read_only=False, risk="high",
            params=["to", "subject", "body"], required=["to", "subject", "body"]),
))
DRIVE = CatalogTool(id="google_drive", name="Google Drive", actions=(
    _action("list_files", "Liệt kê file trong thư mục", read_only=True, params=["folder_id"]),
    _action("read_file", "Đọc nội dung một file", read_only=True, params=["file_id"], required=["file_id"]),
    _action("create_document", "Tạo tài liệu mới trong Drive", read_only=False, risk="medium",
            params=["title", "content"], required=["title", "content"]),
))
CRM = CatalogTool(id="crm", name="CRM nội bộ", actions=(
    _action("get_customer", "Tra thông tin khách hàng", read_only=True, params=["customer_id"], required=["customer_id"]),
    _action("update_deal_stage", "Đổi giai đoạn của một deal", read_only=False, risk="medium",
            params=["deal_id", "stage"], required=["deal_id", "stage"]),
))
ERP = CatalogTool(id="erp", name="ERP tài chính", actions=(
    _action("query_invoice", "Tra một hoá đơn theo mã", read_only=True, params=["invoice_id"], required=["invoice_id"]),
    _action("approve_payment", "Duyệt chi một khoản thanh toán — không hoàn tác", read_only=False, risk="high",
            params=["invoice_id", "amount"], required=["invoice_id", "amount"]),
))
CALENDAR = CatalogTool(id="calendar", name="Google Calendar", actions=(
    _action("list_events", "Liệt kê sự kiện trong khoảng thời gian", read_only=True, params=["from_date", "to_date"]),
    _action("create_event", "Tạo một sự kiện mới, mời người tham dự", read_only=False, risk="medium",
            params=["title", "start", "attendees"], required=["title", "start", "attendees"]),
))
SLACK = CatalogTool(id="slack", name="Slack", actions=(
    _action("post_message", "Đăng một tin nhắn vào kênh", read_only=False, risk="low",
            params=["channel", "text"], required=["channel", "text"]),
))

SCENARIOS: dict[str, dict[str, Any]] = {}


def _spec(name, *, goal, users, data, actions, outcome, criteria, prohibitions, catalog=()):
    SCENARIOS[name] = {
        "spec": {
            "goal": goal, "users": users, "data": data, "actions": actions,
            "expected_outcome": outcome, "success_criteria": criteria, "prohibitions": prohibitions,
        },
        "catalog": catalog,
    }


_spec("01-hoi-dap-thuan-tuy",
      goal="Trả lời câu hỏi của nhân viên mới dựa trên sổ tay nội bộ, không có action nào",
      users=["Nhân viên mới"],
      data=[{"name": "Sổ tay nhân viên", "kind": "uploaded_file", "ref": "blob://so-tay.pdf"}],
      actions=["Tra sổ tay và trả lời"],
      outcome=["Một câu trả lời có trích nguồn"],
      criteria=[{"text": "Nói rõ khi sổ tay không có câu trả lời", "category": "correctness"}],
      prohibitions=["Không bịa chính sách không có trong sổ tay"])

_spec("02-tu-van-thuan-suy-luan",
      goal="Tư vấn quy trình xin nghỉ phép bằng lời, không tra cứu gì, không có tài liệu hay kết nối nào",
      users=["Nhân viên"],
      data=[{"name": "Câu hỏi của nhân viên", "kind": "user_input", "ref": None}],
      actions=["Giải thích quy trình xin nghỉ phép theo kinh nghiệm chung"],
      outcome=["Một đoạn giải thích ngắn gọn"],
      criteria=[{"text": "Không bịa số ngày phép cụ thể của công ty", "category": "correctness"}],
      prohibitions=["Không tự nhận là chính sách chính thức của công ty"])

_spec("03-doc-only-single-connection",
      goal="Tìm các thư trong hộp Gmail theo từ khoá người dùng đưa và liệt kê các thư tìm được",
      users=["Nhân viên văn phòng"],
      data=[{"name": "Hộp thư công việc", "kind": "connected_account", "ref": "conn://gmail-1"}],
      actions=["Tìm thư theo từ khoá"],
      outcome=["Danh sách thư khớp từ khoá"],
      criteria=[{"text": "Chỉ liệt kê thư thật sự trả về, không suy diễn thêm", "category": "correctness"}],
      prohibitions=["Không tự tóm tắt nội dung thư nếu không được yêu cầu"],
      catalog=(GMAIL,))

_spec("04-write-high-risk-mandatory-approval",
      goal="Soạn và gửi email trả lời một yêu cầu của khách, phải có người duyệt trước khi gửi vì gửi không thu hồi được",
      users=["Nhân viên chăm sóc khách hàng"],
      data=[{"name": "Hộp thư hỗ trợ", "kind": "connected_account", "ref": "conn://gmail-2"}],
      actions=["Đọc yêu cầu", "Soạn thư trả lời", "Gửi thư"],
      outcome=["Thư đã gửi, có người duyệt trước"],
      criteria=[{"text": "Không gửi khi chưa có người duyệt", "category": "human_approval_compliance"}],
      prohibitions=["Không tự ý gửi mà không qua duyệt"],
      catalog=(GMAIL,))

_spec("05-write-medium-risk-advised-approval",
      goal="Tạo tài liệu tổng hợp báo cáo tuần trong Google Drive từ dữ liệu đã có",
      users=["Trưởng nhóm"],
      data=[{"name": "Thư mục báo cáo", "kind": "connected_account", "ref": "conn://drive-1"}],
      actions=["Liệt kê file trong thư mục", "Đọc nội dung từng file", "Tạo tài liệu tổng hợp"],
      outcome=["Một tài liệu tổng hợp mới trong Drive"],
      criteria=[{"text": "Tổng hợp đúng số liệu có trong các file nguồn", "category": "correctness"}],
      prohibitions=["Không bịa số liệu không có trong file nguồn"],
      catalog=(DRIVE,))

_spec("06-read-then-write-two-steps",
      goal="Tra cứu thông tin khách hàng trong CRM rồi cập nhật giai đoạn deal của khách đó",
      users=["Nhân viên sales"],
      data=[{"name": "CRM nội bộ", "kind": "connected_account", "ref": "conn://crm-1"}],
      actions=["Tra thông tin khách hàng", "Đổi giai đoạn deal theo yêu cầu người dùng"],
      outcome=["Deal đã được cập nhật đúng giai đoạn"],
      criteria=[{"text": "Chỉ đổi giai đoạn khi người dùng nói rõ giai đoạn mới", "category": "correctness"}],
      prohibitions=["Không tự suy đoán giai đoạn mới nếu người dùng không nói"],
      catalog=(CRM,))

_spec("07-hai-ket-noi-khac-nhau",
      goal="Đọc file đính kèm trong Drive rồi gửi email tóm tắt nội dung file đó cho người nhận do người dùng chỉ định",
      users=["Trợ lý hành chính"],
      data=[
          {"name": "Thư mục tài liệu", "kind": "connected_account", "ref": "conn://drive-2"},
          {"name": "Hộp thư công việc", "kind": "connected_account", "ref": "conn://gmail-3"},
      ],
      actions=["Đọc file trong Drive", "Soạn email tóm tắt", "Gửi email"],
      outcome=["Một email tóm tắt đã gửi, có người duyệt trước khi gửi"],
      criteria=[{"text": "Tóm tắt đúng nội dung file, không thêm bớt", "category": "correctness"}],
      prohibitions=["Không gửi khi chưa có người duyệt"],
      catalog=(DRIVE, GMAIL))

_spec("08-file-va-ket-noi-tron-lan",
      goal="Đối chiếu số liệu trong file ngân sách đã tải lên với hoá đơn thật tra trong ERP, báo cáo chỗ lệch",
      users=["Kế toán"],
      data=[
          {"name": "File ngân sách", "kind": "uploaded_file", "ref": "blob://ngan-sach.xlsx"},
          {"name": "Hệ thống ERP", "kind": "connected_account", "ref": "conn://erp-1"},
      ],
      actions=["Tra hoá đơn theo mã trong ERP"],
      outcome=["Một báo cáo nêu các khoản lệch giữa file và ERP"],
      criteria=[{"text": "Nêu đúng mã hoá đơn bị lệch", "category": "correctness"}],
      prohibitions=["Không tự sửa số liệu trong file gốc"],
      catalog=(ERP,))

_spec("09-duyet-chi-rui-ro-cao",
      goal="Tra một hoá đơn trong ERP rồi duyệt chi khoản thanh toán tương ứng, việc duyệt chi không thể hoàn tác",
      users=["Kế toán trưởng"],
      data=[{"name": "Hệ thống ERP", "kind": "connected_account", "ref": "conn://erp-2"}],
      actions=["Tra hoá đơn theo mã", "Duyệt chi khoản thanh toán"],
      outcome=["Khoản thanh toán đã được duyệt, có người xác nhận trước"],
      criteria=[{"text": "Không tự duyệt chi mà không hỏi người", "category": "human_approval_compliance"}],
      prohibitions=["Không tự động duyệt chi khi chưa có người xác nhận"],
      catalog=(ERP,))

_spec("10-lap-cho-toi-khi-du-du-lieu",
      goal="Liệt kê toàn bộ file trong một thư mục Drive có thể có nhiều trang, đọc lần lượt từng file cho tới khi hết, rồi tổng hợp báo cáo",
      users=["Nhân viên văn phòng"],
      data=[{"name": "Thư mục dự án", "kind": "connected_account", "ref": "conn://drive-3"}],
      actions=["Liệt kê file", "Đọc từng file", "Tổng hợp báo cáo"],
      outcome=["Một báo cáo tổng hợp toàn bộ file trong thư mục"],
      criteria=[{"text": "Không bỏ sót file nào trong thư mục", "category": "correctness"}],
      prohibitions=["Không dừng giữa chừng khi còn file chưa đọc"],
      catalog=(DRIVE,))

_spec("11-re-nhanh-theo-muc-do-khan",
      goal="Đọc yêu cầu hỗ trợ từ khách; nếu là sự cố khẩn cấp thì đăng cảnh báo lên Slack ngay, nếu không thì chỉ trả lời tóm tắt cho người dùng",
      users=["Nhân viên trực hỗ trợ"],
      data=[{"name": "Kênh Slack vận hành", "kind": "connected_account", "ref": "conn://slack-1"}],
      actions=["Đọc yêu cầu", "Đăng cảnh báo lên Slack nếu khẩn cấp"],
      outcome=["Cảnh báo đã đăng nếu khẩn cấp, hoặc một câu trả lời tóm tắt nếu không"],
      criteria=[{"text": "Chỉ đăng Slack khi yêu cầu thực sự khẩn cấp", "category": "correctness"}],
      prohibitions=["Không đăng Slack cho yêu cầu không khẩn cấp"],
      catalog=(SLACK,))

_spec("12-quy-trinh-nhieu-buoc-phuc-tap",
      goal=(
          "Xử lý yêu cầu đặt lịch họp qua email: đọc email yêu cầu, tra lịch hiện có trong Calendar để tìm khung "
          "giờ trống, tạo sự kiện mời người tham dự, rồi gửi email xác nhận lại cho người yêu cầu"
      ),
      users=["Trợ lý điều hành"],
      data=[
          {"name": "Hộp thư điều hành", "kind": "connected_account", "ref": "conn://gmail-4"},
          {"name": "Lịch điều hành", "kind": "connected_account", "ref": "conn://calendar-1"},
      ],
      actions=["Đọc email yêu cầu", "Tra lịch trống", "Tạo sự kiện mời người tham dự", "Gửi email xác nhận"],
      outcome=["Một sự kiện đã tạo và một email xác nhận đã gửi"],
      criteria=[
          {"text": "Chỉ chọn khung giờ thực sự trống trên lịch", "category": "correctness"},
          {"text": "Có người duyệt trước khi tạo sự kiện", "category": "human_approval_compliance"},
      ],
      prohibitions=["Không tự đặt lịch đè lên sự kiện đã có"],
      catalog=(GMAIL, CALENDAR))

_spec("13-khong-ket-noi-chi-co-tai-lieu-va-tim-web",
      goal="So sánh chính sách bảo hành trong tài liệu nội bộ với thông tin công khai trên web rồi nêu điểm khác biệt",
      users=["Nhân viên CSKH"],
      data=[{"name": "Chính sách bảo hành nội bộ", "kind": "uploaded_file", "ref": "blob://bao-hanh.pdf"}],
      actions=["Tìm trên web thông tin bảo hành công khai của đối thủ"],
      outcome=["Một bảng so sánh hai chính sách"],
      criteria=[{"text": "Trích đúng nguồn cho từng điểm khác biệt", "category": "source_groundedness"}],
      prohibitions=["Không bịa thông tin không tìm thấy"])

_spec("14-toi-thieu-mot-hanh-dong-doc",
      goal="Cho biết trạng thái tài khoản của khách hàng khi được hỏi",
      users=["Nhân viên hỗ trợ"],
      data=[{"name": "CRM nội bộ", "kind": "connected_account", "ref": "conn://crm-2"}],
      actions=["Tra thông tin khách hàng theo mã"],
      outcome=["Một câu trả lời nêu đúng trạng thái tài khoản"],
      criteria=[{"text": "Nêu đúng mã khách hàng đã tra", "category": "correctness"}],
      prohibitions=["Không suy đoán trạng thái nếu tra không ra kết quả"],
      catalog=(CRM,))


async def _run_one(name: str, entry: dict, repeat: int) -> dict:
    business_spec = AgentSolutionSpecification.model_validate(entry["spec"])
    started = time.perf_counter()
    try:
        graph = await compose_workflow(
            spec=business_spec,
            model_factory=_build_chat_model,
            model_allowlist=DEFAULT_ALLOWLIST,
            catalog=entry["catalog"],
        )
        return {
            "name": name, "repeat": repeat, "ok": True,
            "nodes": len(graph.nodes),
            "types": sorted({n.type.value for n in graph.nodes}),
            "seconds": time.perf_counter() - started,
            "reason": None,
        }
    except GraphCompositionError as exc:
        return {
            "name": name, "repeat": repeat, "ok": False,
            "nodes": 0, "types": [], "seconds": time.perf_counter() - started,
            "reason": exc.reason,
        }


async def _run(names: list[str] | None, repeats: int) -> int:
    items = [(n, e) for n, e in SCENARIOS.items() if not names or any(n.startswith(prefix) for prefix in names)]
    if not items:
        print(f"Không có kịch bản nào khớp {names!r}. Danh sách: {', '.join(SCENARIOS)}")
        return 1
    print(f"{len(items)} kịch bản x {repeats} lần lặp = {len(items) * repeats} lần soạn\n")

    results = []
    for name, entry in items:
        for r in range(1, repeats + 1):
            row = await _run_one(name, entry, r)
            results.append(row)
            mark = "OK " if row["ok"] else "FAIL"
            detail = ",".join(row["types"]) if row["ok"] else row["reason"]
            print(f"[{mark}] {name:<38} lần {r}  {row['nodes']:>2} node  {row['seconds']:>5.1f}s  {detail[:90]}")

    failed = [r for r in results if not r["ok"]]
    print(f"\nTỔNG: {len(results) - len(failed)}/{len(results)} thành công")
    if failed:
        print("\nTHẤT BẠI:")
        for r in failed:
            print(f"  - {r['name']} (lần {r['repeat']}): {r['reason']}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("prefixes", nargs="*", help="Chỉ chạy kịch bản có tên bắt đầu bằng một trong các tiền tố này")
    parser.add_argument("--repeats", type=int, default=1, help="Chạy mỗi kịch bản bao nhiêu lần (mặc định 1)")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.prefixes or None, args.repeats))


if __name__ == "__main__":
    raise SystemExit(main())
