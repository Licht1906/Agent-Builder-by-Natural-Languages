import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Markdown from "./Markdown";

/* Model viết Markdown vì nó tin có ai đó dựng ra. Khung chat trước đây in
 * nguyên văn, nên một câu trả lời thật hiện thành `- **Thời gian:** 19h`.
 * Bộ dựng này cố ý hẹp — xem chú thích đầu Markdown.jsx — nên bài test
 * canh cả hai phía: cái gì được dựng, và cái gì phải được để yên. */
describe("Markdown", () => {
  it("dựng **đậm** thành đậm thật, không còn dấu sao", () => {
    const { container } = render(<Markdown text="- **Thời gian:** 19h" />);

    expect(screen.getByText("Thời gian:").tagName).toBe("STRONG");
    expect(container.textContent).toBe("- Thời gian: 19h");
  });

  it("dựng *nghiêng* và `mã`", () => {
    render(<Markdown text="Dùng *nhẹ* và `gmail.send`" />);

    expect(screen.getByText("nhẹ").tagName).toBe("EM");
    expect(screen.getByText("gmail.send").tagName).toBe("CODE");
  });

  it("lột dấu # của tiêu đề và để lại một dòng đậm", () => {
    const { container } = render(<Markdown text="## Chi tiết sự kiện" />);

    expect(screen.getByText("Chi tiết sự kiện").tagName).toBe("STRONG");
    expect(container.textContent).toBe("Chi tiết sự kiện");
  });

  it("không đụng vào gạch dưới — node_id phải còn nguyên", () => {
    const { container } = render(<Markdown text="xem run_approvals.caller và agent_llm" />);

    expect(container.textContent).toBe("xem run_approvals.caller và agent_llm");
    expect(container.querySelector("em")).toBeNull();
  });

  it("để yên dấu sao lẻ và cặp chưa đóng", () => {
    const { container } = render(<Markdown text="2*3 = 6 và **chưa đóng" />);

    expect(container.textContent).toBe("2*3 = 6 và **chưa đóng");
    expect(container.querySelector("strong")).toBeNull();
  });

  it("giữ nguyên xuống dòng để pre-wrap của bong bóng lo phần còn lại", () => {
    // Ngoặc nhọn chứ không phải ngoặc kép: thuộc tính JSX không xử lý escape,
    // nên `text="a\nb"` truyền vào đúng hai ký tự gạch chéo và chữ n.
    const { container } = render(<Markdown text={"một\nhai\n\nba"} />);

    expect(container.textContent).toBe("một\nhai\n\nba");
    expect(container.querySelector("br")).toBeNull();
  });

  it("không dựng gì cho chuỗi rỗng", () => {
    const { container } = render(<Markdown text="" />);

    expect(container.textContent).toBe("");
  });
});
