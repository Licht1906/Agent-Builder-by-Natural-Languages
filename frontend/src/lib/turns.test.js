import { describe, expect, it } from "vitest";
import { decided, newTurn, runReducer, settle, turnsFromHistory } from "./turns";

/* Chuỗi sự kiện dưới đây là của một lần chạy thật trên agent "mời tiệc"
 * (extract → if_else → draft → duyệt ‖ duyệt → gmail.send_email → tóm tắt →
 * end), chép từ SSE. Bài test canh một điều: stream sống và lịch sử dựng lại
 * phải ra cùng một lượt — câu trả lời, các bước gấp, chip hoạt động, thẻ duyệt. */

const DRAFT = "**Tiêu đề:** Mời bạn tham dự tiệc\n\nKính gửi Bạn Nam,";
const SUMMARY = "Email đã được gửi thành công.";
const PAYLOAD = '{"message_id": "1a028499e2e4ba41", "sent": true}';

function play(apply, turn, events) {
  return events.reduce((current, [name, data]) => apply(current, name, data), turn);
}

const FIRST_LEG = [
  ["thread", { conversation_id: "c1" }],
  ["node_started", { node_id: "extract_info", node_type: "agent_llm" }],
  ["token", { node_id: "extract_info", text: "- Thời gian: " }],
  ["token", { node_id: "extract_info", text: "19h" }],
  ["node", { node_id: "extract_info", node_type: "agent_llm", content: "- Thời gian: 19h" }],
  ["node_started", { node_id: "check_info_complete", node_type: "if_else" }],
  ["token", { node_id: "check_info_complete", text: '{"holds": true}' }],
  ["node", { node_id: "check_info_complete", node_type: "if_else", content: "đủ", port_taken: "true" }],
  ["node_started", { node_id: "draft_email", node_type: "agent_llm" }],
  ["token", { node_id: "draft_email", text: DRAFT }],
  ["node", { node_id: "draft_email", node_type: "agent_llm", content: DRAFT }],
  ["node_started", { node_id: "approval_send_email", node_type: "human_approval" }],
  ["awaiting_approval", { approval_id: 610, node_id: "approval_send_email", approver_role: "người dùng", message: `Vui lòng kiểm tra:\n\n${DRAFT}` }],
  ["cost", { usd: 0.01 }],
];

const SECOND_LEG = [
  ["thread", { conversation_id: "c1" }],
  ["node_started", { node_id: "approval_send_email", node_type: "human_approval" }],
  ["node", { node_id: "approval_send_email", node_type: "human_approval", content: "Người duyệt đã đồng ý", port_taken: "approved" }],
  ["node_started", { node_id: "send_email", node_type: "mcp" }],
  ["tool", { node_id: "send_email", tool: "gmail.send_email", args: { to: "a@b.c" }, simulated: false }],
  ["tool_result", { node_id: "send_email", tool: "gmail.send_email", ok: true, result: PAYLOAD, simulated: false }],
  ["node", { node_id: "send_email", node_type: "mcp", content: PAYLOAD }],
  ["node_started", { node_id: "summarize_result", node_type: "agent_llm" }],
  ["token", { node_id: "summarize_result", text: SUMMARY }],
  ["node", { node_id: "summarize_result", node_type: "agent_llm", content: SUMMARY }],
  ["node_started", { node_id: "end_success", node_type: "end" }],
  ["node", { node_id: "end_success", node_type: "end", content: "" }],
  ["cost", { usd: 0.02 }],
];

describe("runReducer — lượt sống", () => {
  it("JSON của router không bao giờ lên bong bóng", () => {
    const turn = play(runReducer(), newTurn(), FIRST_LEG.slice(0, 7));
    expect(turn.text).toBe("- Thời gian: 19h");
    expect(turn.text).not.toContain("holds");
  });

  it("node sau lên tiếng thì lời node trước gấp lại, không mất", () => {
    const turn = play(runReducer(), newTurn(), FIRST_LEG.slice(0, 11));
    expect(turn.text).toBe(DRAFT);
    expect(turn.steps).toEqual(["- Thời gian: 19h"]);
  });

  it("dừng chờ duyệt: bản nháp gấp vào steps, thẻ duyệt mang nguyên văn, lượt không còn pending", () => {
    const turn = play(runReducer(), newTurn(), FIRST_LEG);
    expect(turn.pending).toBe(false);
    expect(turn.text).toBe("");
    expect(turn.steps).toEqual(["- Thời gian: 19h", DRAFT]);
    expect(turn.approval).toMatchObject({ id: 610, role: "người dùng", decided: false, approved: null });
    expect(turn.approval.message).toContain(DRAFT);
  });

  it("nửa sau chạy vào cùng lượt: quyết định và tool thành chip, tóm tắt thành câu trả lời, JSON không ở đâu cả", () => {
    const paused = play(runReducer(), newTurn(), FIRST_LEG);
    const resumed = settle(play(runReducer(), decided(paused, true), SECOND_LEG));

    expect(resumed.pending).toBe(false);
    expect(resumed.text).toBe(SUMMARY);
    expect(resumed.approval).toMatchObject({ decided: true, approved: true });
    expect(resumed.activity).toEqual([
      { kind: "decision", ok: true, text: "Người duyệt đã đồng ý" },
      { kind: "tool", id: "send_email", tool: "gmail.send_email", args: { to: "a@b.c" }, simulated: false, ok: true, result: PAYLOAD },
    ]);
    const everything = JSON.stringify([resumed.text, resumed.steps]);
    expect(everything).not.toContain("message_id");
    expect(everything).not.toContain("Người duyệt");
  });

  it("bản chốt của node là thẩm quyền — model không stream vẫn có câu trả lời", () => {
    const turn = play(runReducer(), newTurn(), [
      ["node_started", { node_id: "a", node_type: "agent_llm" }],
      ["node", { node_id: "a", node_type: "agent_llm", content: "trọn câu" }],
    ]);
    expect(turn.text).toBe("trọn câu");
  });

  it("V1: bước là lời nói, token theo step_id", () => {
    const turn = settle(play(runReducer(), newTurn(), [
      ["step_started", { step_id: "s1", title: "Trả lời" }],
      ["token", { step_id: "s1", text: "Xin " }],
      ["token", { step_id: "s1", text: "chào" }],
      ["tool", { step_id: "s1", tool: "web_search", args: { q: "x" }, simulated: false }],
      ["tool_result", { step_id: "s1", tool: "web_search", ok: true, result: "[]", simulated: false }],
      ["step", { step_id: "s1", title: "Trả lời", content: "Xin chào" }],
    ]));
    expect(turn.text).toBe("Xin chào");
    expect(turn.activity).toHaveLength(1);
    expect(turn.activity[0]).toMatchObject({ kind: "tool", tool: "web_search", ok: true });
  });

  it("knowledge_search đi vào sources, không vào activity", () => {
    const turn = play(runReducer(), newTurn(), [
      ["tool_result", { node_id: "k", tool: "knowledge_search", ok: true, result: "…", citations: [{ doc: "x" }] }],
    ]);
    expect(turn.sources).toEqual([{ ok: true, result: "…", citations: [{ doc: "x" }] }]);
    expect(turn.activity).toEqual([]);
  });

  it("data_query cũng đi vào sources — trích dẫn của Hệ CSDL hiện như của tài liệu", () => {
    const citation = { doc_name: "ERP staging · v_ar_open", section_path: "lọc: days_overdue > 30", page_number: null, text: "C1 | 45" };
    const turn = play(runReducer(), newTurn(), [
      ["tool", { node_id: "q", tool: "data_query", args: { view: "v_ar_open" } }],
      ["tool_result", { node_id: "q", tool: "data_query", ok: true, result: "C1 | 45", citations: [citation] }],
    ]);
    expect(turn.sources).toEqual([{ ok: true, result: "C1 | 45", citations: [citation] }]);
  });

  it("tool thất bại và không ai nói sau đó: lượt nói lý do, đánh dấu lỗi", () => {
    const turn = settle(play(runReducer(), newTurn(), [
      ["tool", { node_id: "m", tool: "gmail.send_email", args: {} }],
      ["tool_result", { node_id: "m", tool: "gmail.send_email", ok: false, result: "Thiếu người nhận (to)" }],
    ]));
    expect(turn.failed).toBe(true);
    expect(turn.text).toBe("Thiếu người nhận (to)");
    expect(turn.activity[0]).toMatchObject({ kind: "tool", ok: false });
  });

  it("tool thất bại rồi agent vẫn nói: câu trả lời là lời agent, chip giữ lỗi", () => {
    const turn = settle(play(runReducer(), newTurn(), [
      ["tool", { node_id: "m", tool: "gmail.send_email", args: {} }],
      ["tool_result", { node_id: "m", tool: "gmail.send_email", ok: false, result: "lỗi" }],
      ["node_started", { node_id: "a", node_type: "agent_llm" }],
      ["token", { node_id: "a", text: "Không gửi được, bạn kiểm tra lại." }],
    ]));
    expect(turn.failed).toBe(false);
    expect(turn.text).toBe("Không gửi được, bạn kiểm tra lại.");
  });

  it("mcp → end: không lời nào, lượt im lặng nhưng có chip tool để màn hình diễn đạt", () => {
    const turn = settle(play(runReducer(), newTurn(), [
      ["tool", { node_id: "m", tool: "gmail.send_email", args: {} }],
      ["tool_result", { node_id: "m", tool: "gmail.send_email", ok: true, result: PAYLOAD }],
      ["node", { node_id: "m", node_type: "mcp", content: PAYLOAD }],
      ["node", { node_id: "e", node_type: "end", content: "" }],
    ]));
    expect(turn.text).toBe("");
    expect(turn.failed).toBe(false);
    expect(turn.activity[0]).toMatchObject({ kind: "tool", ok: true, result: PAYLOAD });
  });

  it("lỗi run: lượt lỗi, câu là thông điệp", () => {
    const turn = settle(play(runReducer(), newTurn(), [["error", { message: "Model không trả lời" }]]));
    expect(turn.failed).toBe(true);
    expect(turn.text).toBe("Model không trả lời");
  });

  it("dừng vì ngân sách sau khi đã nói: chip cảnh báo, lời giữ nguyên", () => {
    const turn = settle(play(runReducer(), newTurn(), [
      ["node_started", { node_id: "a", node_type: "agent_llm" }],
      ["token", { node_id: "a", text: "một nửa" }],
      ["budget_exceeded", { message: "Hết ngân sách token" }],
    ]));
    expect(turn.text).toBe("một nửa");
    expect(turn.activity).toEqual([{ kind: "stopped", text: "Hết ngân sách token" }]);
  });
});

describe("turnsFromHistory — lịch sử dựng lại y như lượt sống", () => {
  const HISTORY = [
    { role: "user", content: "Mời dự tiệc…" },
    { role: "agent", content: "- Thời gian: 19h", node_id: "extract_info", node_type: "agent_llm" },
    { role: "agent", content: DRAFT, node_id: "draft_email", node_type: "agent_llm" },
    { role: "agent", content: "Người duyệt đã đồng ý", node_id: "approval_send_email", node_type: "human_approval", ok: true },
    { role: "agent", content: PAYLOAD, node_id: "send_email", node_type: "mcp", tool: "gmail.send_email", ok: true },
    { role: "agent", content: SUMMARY, node_id: "summarize_result", node_type: "agent_llm" },
  ];

  it("một cặp user/agent, JSON và lời duyệt thành chip, tóm tắt là câu trả lời", () => {
    const [user, turn] = turnsFromHistory(HISTORY);
    expect(user).toEqual({ from: "user", text: "Mời dự tiệc…" });
    expect(turn.text).toBe(SUMMARY);
    expect(turn.steps).toEqual(["- Thời gian: 19h", DRAFT]);
    expect(turn.activity).toEqual([
      { kind: "decision", ok: true, text: "Người duyệt đã đồng ý" },
      { kind: "tool", id: "send_email", tool: "gmail.send_email", simulated: false, ok: true, result: PAYLOAD },
    ]);
    expect(turn.pending).toBe(false);
    expect(turn.approval).toBeNull();
  });

  it("khớp với lượt sống cùng lần chạy, phần nhìn thấy", () => {
    const live = settle(play(runReducer(), decided(play(runReducer(), newTurn(), FIRST_LEG), true), SECOND_LEG));
    const [, replayed] = turnsFromHistory(HISTORY);
    expect(replayed.text).toBe(live.text);
    expect(replayed.steps).toEqual(live.steps);
    expect(replayed.activity.map((item) => [item.kind, item.ok])).toEqual(live.activity.map((item) => [item.kind, item.ok]));
  });

  it("hội thoại đang dừng chờ duyệt: thẻ duyệt ở lượt cuối, bản nháp gấp lại", () => {
    const pending = { approval_id: 610, node_id: "approval_send_email", approver_role: "người dùng", message: `Vui lòng kiểm tra:\n\n${DRAFT}` };
    const turns = turnsFromHistory(HISTORY.slice(0, 3), pending);
    const last = turns[turns.length - 1];
    expect(last.text).toBe("");
    expect(last.steps).toEqual(["- Thời gian: 19h", DRAFT]);
    expect(last.approval).toMatchObject({ id: 610, decided: false });
  });

  it("thẻ duyệt mở lại vẫn mang nguyên lời gọi sẽ thực hiện (DS-08)", () => {
    const actions = [{ node_id: "send", tool: "gmail.send_email", action: "send_email", args: { to: "a@b.c", body: "x" }, hash: "h" }];
    const pending = { approval_id: 610, node_id: "approval_send_email", approver_role: "", message: "Duyệt?", actions };
    const turns = turnsFromHistory(HISTORY.slice(0, 3), pending);
    expect(turns[turns.length - 1].approval.actions).toEqual(actions);
  });

  it("dòng không nhãn (V1, checkpoint cũ) là lời nói — như trước nay", () => {
    const [, turn] = turnsFromHistory([
      { role: "user", content: "hỏi" },
      { role: "agent", content: "đáp", step_id: "s1" },
    ]);
    expect(turn.text).toBe("đáp");
  });

  it("lời từ chối không nhãn ok vẫn đọc ra là từ chối", () => {
    const [, turn] = turnsFromHistory([
      { role: "user", content: "x" },
      { role: "agent", content: "Người duyệt đã từ chối: sai người nhận", node_type: "human_approval" },
    ]);
    expect(turn.activity[0]).toMatchObject({ kind: "decision", ok: false });
  });

  it("nhiều cặp: mỗi user mở một lượt mới", () => {
    const turns = turnsFromHistory([
      { role: "user", content: "một" },
      { role: "agent", content: "A", node_type: "agent_llm" },
      { role: "user", content: "hai" },
      { role: "agent", content: "B", node_type: "agent_llm" },
    ]);
    expect(turns.map((turn) => turn.text)).toEqual(["một", "A", "hai", "B"]);
  });
});

describe("collect — node thu thập (ADR-0052) là người nói", () => {
  const QUESTION = "Bạn cho tôi xin email người nhận nhé?";

  it("câu hỏi khi còn thiếu là câu trả lời của lượt, stream lẫn lịch sử", () => {
    const live = play(runReducer(), newTurn(), [
      ["node_started", { node_id: "thu_thap", node_type: "collect" }],
      ["token", { node_id: "thu_thap", text: "Bạn cho tôi xin " }],
      ["token", { node_id: "thu_thap", text: "email người nhận nhé?" }],
      ["node", { node_id: "thu_thap", node_type: "collect", content: QUESTION, port_taken: "incomplete" }],
      ["node_started", { node_id: "xong", node_type: "end" }],
      ["node", { node_id: "xong", node_type: "end", content: "" }],
    ]);
    expect(live.text).toBe(QUESTION);
    expect(live.steps).toEqual([]);

    const [turn] = turnsFromHistory([{ role: "agent", content: QUESTION, node_id: "thu_thap", node_type: "collect" }]);
    expect(turn.text).toBe(QUESTION);
  });

  it("dòng tóm tắt khi đã đủ gấp vào steps khi node sau lên tiếng", () => {
    const SUMMARY_LINES = "to: lan@example.com\nsubject: Họp";
    const live = play(runReducer(), newTurn(), [
      ["node_started", { node_id: "thu_thap", node_type: "collect" }],
      ["node", { node_id: "thu_thap", node_type: "collect", content: SUMMARY_LINES, port_taken: "complete" }],
      ["node_started", { node_id: "ke", node_type: "agent_llm" }],
      ["token", { node_id: "ke", text: "Đã gửi." }],
      ["node", { node_id: "ke", node_type: "agent_llm", content: "Đã gửi." }],
    ]);
    expect(live.steps).toEqual([SUMMARY_LINES]);
    expect(live.text).toBe("Đã gửi.");
  });

  it("đi qua mà không nói gì (action không có tham số bắt buộc) thì không để lại bước trống", () => {
    const live = play(runReducer(), newTurn(), [
      ["node_started", { node_id: "thu_thap", node_type: "collect" }],
      ["node", { node_id: "thu_thap", node_type: "collect", content: "", port_taken: "complete" }],
      ["node_started", { node_id: "ke", node_type: "agent_llm" }],
      ["node", { node_id: "ke", node_type: "agent_llm", content: "Có 3 thư." }],
    ]);
    expect(live.steps).toEqual([]);
    expect(live.text).toBe("Có 3 thư.");
  });
});
