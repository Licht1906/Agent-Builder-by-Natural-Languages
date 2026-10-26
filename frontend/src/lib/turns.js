/* ============================================================
   Một lượt của agent — dựng từ stream đang chạy, hoặc dựng lại từ lịch sử.

   Khung chat trước đây không có khái niệm "lượt". Một câu trả lời là một
   chuỗi bị ghi đè theo từng sự kiện, và lịch sử là một danh sách phẳng mọi
   message trong checkpoint — mỗi cái thành một bong bóng "agent nói". Hai
   đường đó không dựng ra cùng một thứ, nên mỗi lần mở lại hội thoại người ta
   thấy những gì stream sống đã giấu: JSON của tool, "Người duyệt đã đồng ý",
   hai bản nháp chồng nhau.

   Ở đây một lượt là một cấu trúc, và **cả hai đường đều phải dựng ra đúng
   cấu trúc ấy**:

     text      câu trả lời — lời của node Agent/LLM nói *sau cùng*
     steps     lời của các node Agent/LLM nói trước đó trong lượt, gấp lại
     activity  những gì run *làm*: gọi tool, quyết định duyệt, dừng vì ngân
               sách, lỗi — vẽ thành chip, không bao giờ thành bong bóng
     approval  thẻ "đang chờ duyệt", với quyết định nếu đã có
     sources   trích dẫn của knowledge_search và data_query (CITING_TOOLS)
     pending   run còn đang chạy vào lượt này
     failed    lượt kết thúc bằng một lỗi

   Không có React ở đây, cố ý: mọi quyết định "cái này hiện ra sao" là một
   hàm thuần trên dữ liệu, và bài test gọi thẳng nó.
   ============================================================ */

/** Những tool mà kết quả mang trích dẫn — hiện ở panel "Nguồn tham khảo",
 *  không thành chip. `data_query` (DS-04) trích dẫn Hệ CSDL + view + bộ lọc
 *  theo đúng hình dạng `Citation` của `knowledge_search`. */
export const CITING_TOOLS = new Set(["knowledge_search", "data_query"]);

/** Một lượt trống, vừa được mở để run viết vào. */
export function newTurn() {
  return { from: "agent", text: "", steps: [], activity: [], approval: null, sources: [], pending: true, failed: false };
}

/** Node nào có quyền *nói* — chữ của nó là câu trả lời.
 *
 *  `agent_llm`, và `collect` (ADR-0052): câu nó hỏi khi còn thiếu thông tin
 *  *là* câu trả lời của lượt, và dòng tóm tắt khi đã đủ là thứ thẻ duyệt in
 *  bên dưới. Một router (`if_else`/`while`) cân nhắc bằng JSON, một node tool
 *  điền tham số bằng JSON; để chúng lên sàn là cách câu trả lời từng bị *thay
 *  bằng JSON* trên màn hình. V1 không có loại node: mọi bước của nó là một lần
 *  model nói, nên `step_started` được ghi là `agent_llm`. */
const SPEAKING = new Set(["agent_llm", "collect"]);

/** Id của node (V2) hay của bước (V1) mà một sự kiện thuộc về. */
const idOf = (data) => data.node_id ?? data.step_id ?? null;

/** Lời đang đứng trong bong bóng được gấp vào `steps` — không bao giờ bị xoá.
 *
 *  Xoá là chỗ hỏng người dùng mô tả là "hiện dữ liệu thu thập rồi xoá đi":
 *  bước trích xuất phát ra một danh sách đúng, bước soạn thảo lên tiếng, và
 *  danh sách đang đọc dở biến mất. Chữ tự dưng mất luôn bị đọc là lỗi. */
function fold(turn) {
  if (!turn.text) return turn;
  return { ...turn, steps: [...turn.steps, turn.text], text: "" };
}

/** Bộ thu một run đang stream thành một lượt.
 *
 *  Một hàm `(turn, name, data) → turn` có trạng thái riêng (loại của từng
 *  node, node nào đang nói), nên mỗi run phải lấy một bộ thu mới. Dùng chung
 *  cho lượt mới lẫn nửa sau của run bị dừng chờ duyệt — với run đó là một
 *  lần chạy, và hai bản của hàm này sẽ lệch nhau ngay ở lần sửa đầu tiên. */
export function runReducer() {
  const kinds = {};
  let speaking = null;

  return function apply(turn, name, data) {
    const id = idOf(data);
    switch (name) {
      case "node_started":
        kinds[id] = data.node_type;
        return turn;
      case "step_started":
        kinds[id] = "agent_llm";
        return turn;

      case "token": {
        const kind = kinds[id];
        if (kind !== undefined && !SPEAKING.has(kind)) return turn;
        if (id !== null && id !== speaking) {
          speaking = id;
          return { ...fold(turn), text: data.text, pending: true };
        }
        return { ...turn, text: turn.text + data.text, pending: true };
      }

      case "node": {
        if (data.node_type === "human_approval") {
          const approved = data.port_taken === "approved";
          return { ...turn, activity: [...turn.activity, { kind: "decision", ok: approved, text: data.content }] };
        }
        if (!SPEAKING.has(data.node_type)) return turn;
        // Bản đầy đủ là bản có thẩm quyền: một model không stream, hay một
        // token rơi dọc đường, đều kết thúc ở đúng nội dung node đã chốt.
        if (id !== speaking) {
          speaking = id;
          return { ...fold(turn), text: data.content ?? "", pending: true };
        }
        return { ...turn, text: data.content ?? turn.text };
      }

      case "step":
        // V1: bước đã chốt câu trả lời của nó — cùng luật với `node` ở trên.
        if (id !== speaking) {
          speaking = id;
          return { ...fold(turn), text: data.content ?? "", pending: true };
        }
        return { ...turn, text: data.content ?? turn.text };

      case "tool":
        return {
          ...turn,
          activity: [...turn.activity, { kind: "tool", id, tool: data.tool, args: data.args, simulated: Boolean(data.simulated), ok: null, result: null }],
        };

      case "tool_result": {
        if (CITING_TOOLS.has(data.tool)) {
          const entry = { ok: data.ok, result: data.result, citations: data.citations || [] };
          return { ...turn, sources: [...turn.sources, entry] };
        }
        const open = turn.activity.findLastIndex((item) => item.kind === "tool" && item.id === id && item.tool === data.tool && item.ok === null);
        const settled = { kind: "tool", id, tool: data.tool, simulated: Boolean(data.simulated), ok: data.ok !== false, result: data.result ?? "" };
        if (open === -1) return { ...turn, activity: [...turn.activity, settled] };
        const activity = turn.activity.slice();
        activity[open] = { ...activity[open], ...settled };
        return { ...turn, activity };
      }

      case "awaiting_approval":
        // Bản nháp vừa nói được gấp lại: thứ đứng trước mặt người đọc bây giờ
        // là thẻ duyệt, và thẻ đó đã chứa nguyên văn bản nháp rồi.
        return {
          ...fold(turn),
          pending: false,
          approval: { id: data.approval_id, nodeId: data.node_id, role: data.approver_role || "", message: data.message || "", actions: data.actions || [], decided: false, approved: null },
        };

      case "budget_exceeded":
        return { ...turn, activity: [...turn.activity, { kind: "stopped", text: data.message || "" }] };

      case "error":
        return { ...turn, failed: true, activity: [...turn.activity, { kind: "error", text: data.message || "" }] };

      default:
        // `thread`, `usage`, `cost`, `sandbox_run`: việc của màn hình, không
        // phải của lượt.
        return turn;
    }
  };
}

/** Run đã ngừng viết vào lượt này. Lượt thành phẳng, câu trả lời được chốt.
 *
 *  Một lượt không có lời nào — graph kết thúc bằng một node tool, hay chết
 *  giữa chừng — vẫn phải có gì đó để nói: lỗi cuối cùng nếu có, còn không thì
 *  để trống cho màn hình tự diễn đạt từ `activity` (nó biết cách dịch một
 *  chip tool thành "Đã thực hiện …" theo ngôn ngữ người đọc). */
export function settle(turn) {
  if (turn.text || turn.approval) return { ...turn, pending: false };
  const lastFailure = turn.activity.findLast((item) => item.kind === "error" || (item.kind === "tool" && item.ok === false));
  if (lastFailure) {
    return { ...turn, pending: false, failed: true, text: lastFailure.kind === "tool" ? lastFailure.result : lastFailure.text };
  }
  return { ...turn, pending: false };
}

/** Người duyệt đã quyết; run sắp chạy tiếp vào đúng lượt này. */
export function decided(turn, approved) {
  if (!turn.approval) return turn;
  return { ...turn, pending: true, approval: { ...turn.approval, decided: true, approved } };
}

/** Lịch sử trong checkpoint, dựng lại thành đúng cấu trúc stream sống dựng.
 *
 *  Mỗi dòng `user` mở một cặp mới; mọi dòng agent sau nó cho tới dòng `user`
 *  kế tiếp thuộc cùng một lượt. Trong lượt, `node_type` quyết định dòng là
 *  lời nói hay việc làm — đúng bảng phân loại `runReducer` dùng cho stream:
 *
 *    agent_llm, collect / không nhãn  lời nói → `steps` rồi `text` (lời sau cùng)
 *    mcp                          chip tool, `tool`/`ok` đi kèm dòng
 *    human_approval               chip quyết định
 *    còn lại                      bỏ qua — router và `end` không nói gì
 *
 *  Dòng không có nhãn (V1, hay checkpoint ghi trước khi runtime dán nhãn)
 *  là lời nói, như nó vẫn luôn được coi là thế. */
export function turnsFromHistory(lines, pendingApproval = null) {
  const out = [];
  let current = null;
  for (const line of lines) {
    if (line.role === "user") {
      out.push({ from: "user", text: line.content });
      current = null;
      continue;
    }
    if (!current) {
      current = { ...newTurn(), pending: false };
      out.push(current);
    }
    const kind = line.node_type || "agent_llm";
    if (kind === "mcp") {
      current.activity = [...current.activity, { kind: "tool", id: line.node_id, tool: line.tool || "", simulated: false, ok: line.ok !== false, result: line.content }];
    } else if (kind === "human_approval") {
      current.activity = [...current.activity, { kind: "decision", ok: line.ok !== false && !/từ chối/i.test(line.content), text: line.content }];
    } else if (SPEAKING.has(kind)) {
      if (current.text) current.steps = [...current.steps, current.text];
      current.text = line.content;
    }
  }
  if (pendingApproval) {
    // Run cuối đang dừng: thẻ duyệt đứng ở lượt cuối, bản nháp gấp lại — y
    // như lúc stream sống nhận `awaiting_approval`.
    if (!current) {
      current = { ...newTurn(), pending: false };
      out.push(current);
    }
    const folded = fold(current);
    Object.assign(current, folded, {
      approval: { id: pendingApproval.approval_id, nodeId: pendingApproval.node_id, role: pendingApproval.approver_role || "", message: pendingApproval.message || "", actions: pendingApproval.actions || [], decided: false, approved: null },
    });
  }
  return out.map((turn) => (turn.from === "agent" ? settle(turn) : turn));
}
