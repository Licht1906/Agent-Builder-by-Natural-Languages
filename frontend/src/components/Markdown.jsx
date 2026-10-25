import { Fragment } from "react";

/* ============================================================
   Markdown, cắt xuống đúng phần mô hình thật sự viết ra.

   Chat hiện chữ của model nguyên văn, nên một câu trả lời có
   `- **Thời gian:** 19h` hiện ra đúng như thế — dấu sao và tất cả.
   Người đọc thấy một lỗi hiển thị, và họ đúng: không ai gõ `**`.

   Đây **không phải** một bộ dựng Markdown đầy đủ, và cố ý không phải:

   - Chỉ nhấn mạnh trong dòng — `**đậm**`, `*nghiêng*`, `` `mã` `` — cộng
     với dấu `#` đầu dòng bị lột thành một dòng đậm. Gạch đầu dòng `- `,
     đường kẻ `---` và xuống dòng giữ nguyên: bong bóng chat đã có
     `white-space: pre-wrap` nên chúng vốn đã hiện đúng, và thay chúng
     bằng `<ul>` thật sẽ kéo theo cả một cuộc phẫu thuật khoảng trắng để
     đổi lấy đúng những gạch đầu dòng người ta đang nhìn thấy.
   - Không link, không ảnh, không HTML thô. Chữ ở đây do một mô hình
     sinh ra từ nội dung người lạ gửi tới; một cái `href` dựng từ nguồn
     đó là thứ không đáng đánh đổi để lấy một chữ xanh bấm được.
   - `_gạch_dưới_` **cố tình không được hiểu là nghiêng**. Nền tảng này
     nói suốt về `node_id`, `run_approvals.caller`, `agent_llm` — coi
     gạch dưới là cú pháp thì mỗi tên như thế đều bị bẻ vụn.

   Không có `dangerouslySetInnerHTML` ở đâu cả: mọi thứ trả về là phần
   tử React, nên chữ vẫn chỉ là chữ.
   ============================================================ */

/** `**đậm**`, `*nghiêng*`, `` `mã` `` — theo đúng thứ tự đó.
 *
 *  `**` đứng trước `*` trong nhánh chọn nên chuỗi đậm không bao giờ bị
 *  đọc nhầm thành hai lần nghiêng. Cả hai đều đòi ký tự liền sau dấu mở
 *  không phải khoảng trắng, đó là cái phân biệt `*nghiêng*` với dấu sao
 *  mở đầu một gạch đầu dòng.
 */
const INLINE = /(\*\*(?=\S)[\s\S]+?(?<=\S)\*\*|\*(?=\S)[^*\n]+?(?<=\S)\*|`[^`\n]+`)/;

/** Một dòng đã lột dấu `#`, cắt thành chuỗi và phần tử nhấn mạnh. */
function inline(line) {
  const pieces = line.split(INLINE).filter((piece) => piece !== "" && piece !== undefined);
  return pieces.map((piece, index) => {
    if (piece.startsWith("**") && piece.endsWith("**") && piece.length > 4) {
      return <strong key={index}>{piece.slice(2, -2)}</strong>;
    }
    if (piece.startsWith("`") && piece.endsWith("`") && piece.length > 2) {
      return <code key={index} className="ab-mono">{piece.slice(1, -1)}</code>;
    }
    if (piece.startsWith("*") && piece.endsWith("*") && piece.length > 2) {
      return <em key={index}>{piece.slice(1, -1)}</em>;
    }
    return piece;
  });
}

const HEADING = /^(#{1,6})\s+(.*)$/;

/** Một dòng: tiêu đề `#` thành một dòng đậm, còn lại là nhấn mạnh trong dòng. */
function renderLine(line) {
  const heading = HEADING.exec(line);
  if (heading) return <strong>{inline(heading[2])}</strong>;
  return inline(line);
}

/** Chữ của agent, với phần nhấn mạnh đã thành nhấn mạnh thật.
 *
 *  Xuống dòng trả về nguyên vẹn dưới dạng ký tự `\n` chứ không phải `<br>`:
 *  chỗ gọi đã đặt `white-space: pre-wrap`, và trộn hai cách xuống dòng là
 *  cách chắc chắn nhất để có dòng trống đôi.
 */
export default function Markdown({ text }) {
  if (!text) return null;
  const lines = String(text).split("\n");
  return (
    <>
      {lines.map((line, index) => (
        <Fragment key={index}>
          {index > 0 && "\n"}
          {renderLine(line)}
        </Fragment>
      ))}
    </>
  );
}
