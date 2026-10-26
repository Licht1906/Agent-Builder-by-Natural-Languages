import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "../i18n";
import { AgentTurn, ApprovalCard, StepsTrail } from "./Chat";
import { newTurn } from "../lib/turns";

/* Phần nhìn thấy của một lượt. `lib/turns.test.js` đã canh dữ liệu; ở đây
 * canh những gì người dùng từng thấy sai: JSON trong bong bóng, lời duyệt
 * thành câu của agent, nút duyệt không hiện cho chủ agent, bước trước bị xoá. */

const PAYLOAD = '{"message_id": "1a028499e2e4ba41", "sent": true}';

const draw = (ui) => render(<LanguageProvider>{ui}</LanguageProvider>);

function finished(overrides) {
  return { ...newTurn(), pending: false, ...overrides };
}

describe("AgentTurn", () => {
  it("câu trả lời là lời agent; JSON của tool và lời duyệt chỉ là chip", () => {
    const turn = finished({
      text: "Email đã được gửi thành công.",
      steps: ["- Thời gian: 19h"],
      activity: [
        { kind: "decision", ok: true, text: "Người duyệt đã đồng ý" },
        { kind: "tool", id: "send_email", tool: "gmail.send_email", simulated: false, ok: true, result: PAYLOAD },
      ],
      approval: { id: 610, role: "người dùng", message: "Vui lòng kiểm tra", decided: true, approved: true },
    });
    const { container } = draw(<AgentTurn turn={turn} canDecide={false} busy={false} onDecide={() => {}} />);

    expect(screen.getByText("Email đã được gửi thành công.")).toBeInTheDocument();
    expect(container.textContent).not.toContain("message_id");
    expect(screen.getByText("Người duyệt đã đồng ý")).toBeInTheDocument();
    expect(screen.getByText(/gmail\.send_email/)).toBeInTheDocument();
    expect(screen.getByText(/Approved|Đã duyệt/)).toBeInTheDocument();
  });

  it("chip tool mở ra mới thấy payload", async () => {
    const turn = finished({
      text: "xong",
      activity: [{ kind: "tool", id: "m", tool: "gmail.send_email", simulated: false, ok: true, result: PAYLOAD }],
    });
    const { container } = draw(<AgentTurn turn={turn} canDecide={false} busy={false} onDecide={() => {}} />);
    expect(container.textContent).not.toContain("message_id");

    await userEvent.click(screen.getByRole("button", { name: /gmail\.send_email/ }));

    expect(container.textContent).toContain("message_id");
  });

  it("lượt kết thúc bằng tool mà không ai nói: bong bóng nói 'Đã thực hiện …', không phải JSON", () => {
    const turn = finished({
      activity: [{ kind: "tool", id: "m", tool: "gmail.send_email", simulated: false, ok: true, result: PAYLOAD }],
    });
    const { container } = draw(<AgentTurn turn={turn} canDecide={false} busy={false} onDecide={() => {}} />);

    expect(screen.getByText(/gmail\.send_email/, { selector: "p" })).toBeInTheDocument();
    expect(container.textContent).not.toContain("message_id");
  });

  it("lượt thất bại: bong bóng đỏ mang lý do", () => {
    const turn = finished({ text: "Thiếu người nhận (to)", failed: true, activity: [{ kind: "tool", id: "m", tool: "gmail.send_email", ok: false, result: "Thiếu người nhận (to)" }] });
    draw(<AgentTurn turn={turn} canDecide={false} busy={false} onDecide={() => {}} />);

    const bubble = screen.getByText("Thiếu người nhận (to)", { selector: "p" });
    expect(bubble.style.color).toBe("rgb(248, 113, 113)");
  });

  it("đang nghĩ: bong bóng 'thinking', chưa có chip nào", () => {
    const { container } = draw(<AgentTurn turn={newTurn()} canDecide={false} busy={true} onDecide={() => {}} />);

    expect(container.textContent).toMatch(/thinking|đang nghĩ/i);
  });

  it("markdown được dựng trong câu trả lời", () => {
    const turn = finished({ text: "- **Thời gian:** 19h" });
    draw(<AgentTurn turn={turn} canDecide={false} busy={false} onDecide={() => {}} />);

    expect(screen.getByText("Thời gian:").tagName).toBe("STRONG");
  });
});

describe("ApprovalCard", () => {
  const waiting = { id: 610, role: "người dùng", message: "Vui lòng kiểm tra **nội dung**", decided: false, approved: null };

  it("chủ agent thấy nút Duyệt; người khác thấy lời nhắn chờ", () => {
    const onDecide = vi.fn();
    const owner = draw(<ApprovalCard approval={waiting} canDecide={true} busy={false} onDecide={onDecide} />);
    expect(owner.getByRole("button", { name: /Approve|Duyệt/ })).toBeInTheDocument();
    owner.unmount();

    const other = draw(<ApprovalCard approval={waiting} canDecide={false} busy={false} onDecide={onDecide} />);
    expect(other.queryByRole("button", { name: /Approve|Duyệt/ })).toBeNull();
    expect(other.container.textContent).toMatch(/creator will decide|người tạo/i);
  });

  it("bấm Duyệt gọi onDecide(true)", async () => {
    const onDecide = vi.fn();
    draw(<ApprovalCard approval={waiting} canDecide={true} busy={false} onDecide={onDecide} />);

    await userEvent.click(screen.getByRole("button", { name: /Approve|Duyệt/ }));

    expect(onDecide).toHaveBeenCalledWith(true, "");
  });

  it("từ chối bắt buộc có lý do", async () => {
    const onDecide = vi.fn();
    draw(<ApprovalCard approval={waiting} canDecide={true} busy={false} onDecide={onDecide} />);

    await userEvent.click(screen.getByRole("button", { name: /Reject|Từ chối/ }));
    const confirm = screen.getAllByRole("button", { name: /Reject|Từ chối/ }).at(-1);
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByRole("textbox"), "sai người nhận");
    await userEvent.click(screen.getAllByRole("button", { name: /Reject|Từ chối/ }).at(-1));

    expect(onDecide).toHaveBeenCalledWith(false, "sai người nhận");
  });

  it("đã quyết thì thẻ nói thế và không còn nút", () => {
    draw(<ApprovalCard approval={{ ...waiting, decided: true, approved: true }} canDecide={true} busy={false} onDecide={() => {}} />);

    expect(screen.queryByRole("button", { name: /Approve|Duyệt/ })).toBeNull();
    expect(screen.getByText(/Approved|Đã duyệt/)).toBeInTheDocument();
  });

  it("hiện nguyên văn lời gọi sẽ được thực hiện — người nhận, tiêu đề, nội dung (DS-08)", () => {
    const body = "Chào anh,\n\n**Khiếu nại** đã được xử lý.";
    const actions = [
      { node_id: "send", tool: "gmail.send_email", action: "send_email", args: { to: "khach@example.com", subject: "Về khiếu nại", body }, hash: "h" },
    ];
    draw(<ApprovalCard approval={{ ...waiting, actions }} canDecide={true} busy={false} onDecide={() => {}} />);

    const shown = screen.getByTestId("approval-payload");
    expect(shown).toHaveTextContent("gmail.send_email");
    expect(shown).toHaveTextContent("khach@example.com");
    expect(shown).toHaveTextContent("Về khiếu nại");
    // Verbatim: the asterisks stay — nothing is rendered as Markdown here.
    expect(shown.textContent).toContain("**Khiếu nại** đã được xử lý.");
  });

  it("đã quyết thì không còn hiện lời gọi chờ duyệt", () => {
    const actions = [{ node_id: "send", tool: "gmail.send_email", action: "send_email", args: { to: "a@b.c" }, hash: "h" }];
    draw(<ApprovalCard approval={{ ...waiting, actions, decided: true, approved: true }} canDecide={true} busy={false} onDecide={() => {}} />);

    expect(screen.queryByTestId("approval-payload")).toBeNull();
  });

  it("dựng markdown trong thông điệp duyệt", () => {
    draw(<ApprovalCard approval={waiting} canDecide={false} busy={false} onDecide={() => {}} />);

    expect(screen.getByText("nội dung").tagName).toBe("STRONG");
  });
});

describe("StepsTrail", () => {
  it("đóng sẵn, mở ra thấy đúng chữ của bước trước", async () => {
    draw(<StepsTrail steps={["- **Thời gian:** 19h", "Bản nháp"]} />);

    expect(screen.queryByText("Bản nháp")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /2/ }));

    expect(screen.getByText("Bản nháp")).toBeInTheDocument();
    expect(screen.getByText("Thời gian:").tagName).toBe("STRONG");
  });
});
