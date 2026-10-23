import { useState } from "react";
import { ChevronDown, ChevronRight, FileSearch, SearchX, AlertTriangle } from "lucide-react";

/* ============================================================
   "Nguồn tham khảo" — built from `tool_result` events a `KnowledgeSearch` node
   emitted, never from the model's own prose (RAG-14 / #100, ADR-0044).

   `entries` is one item per `knowledge_search` tool_result this turn produced:
   `{ ok, citations }`. A search that ran and matched nothing is `ok: true`
   with `citations: []` — its own state, not an error. A search that could not
   read its source is `ok: false` — shown as a failure, never as "no sources".
   ============================================================ */
export default function SourcesPanel({ entries }) {
  if (!entries || entries.length === 0) return null;

  const failed = entries.filter((entry) => entry.ok === false);
  const citations = entries.filter((entry) => entry.ok !== false).flatMap((entry) => entry.citations || []);
  const notFound = citations.length === 0 && failed.length === 0;

  return (
    <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
      {citations.length > 0 && <SourceList citations={citations} />}

      {notFound && (
        <p style={{ display: "flex", gap: 6, alignItems: "flex-start", fontSize: "var(--text-xs)", color: "var(--ab-muted)", margin: 0, lineHeight: 1.5 }}>
          <SearchX size={13} style={{ flexShrink: 0, marginTop: 1 }} />
          Không tìm thấy tài liệu nào khớp với câu hỏi này — đây là kết quả tra cứu, không phải lỗi hệ thống.
        </p>
      )}

      {failed.length > 0 && (
        <p style={{ display: "flex", gap: 6, alignItems: "flex-start", fontSize: "var(--text-xs)", color: "var(--ab-warn)", margin: 0, lineHeight: 1.5 }}>
          <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
          {failed.map((entry) => entry.result).join(" ")}
        </p>
      )}
    </div>
  );
}

function SourceList({ citations }) {
  const [open, setOpen] = useState(false);

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{ display: "flex", alignItems: "center", gap: 5, background: "none", border: "none", color: "var(--ab-muted)", fontSize: "var(--text-xs)", cursor: "pointer", padding: 0 }}
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <FileSearch size={12} />
        Nguồn tham khảo ({citations.length})
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
          {citations.map((citation, index) => (
            <div key={index} className="ab-card" style={{ padding: "var(--space-2)", background: "rgba(255,255,255,.015)" }}>
              <p className="ab-mono" style={{ fontSize: "var(--text-2xs)", color: "var(--ab-primary)", margin: 0 }}>
                {citation.section_path || citation.doc_name}
                {citation.page_number != null ? `, trang ${citation.page_number}` : ""}
              </p>
              <p style={{ fontSize: "var(--text-xs)", color: "var(--ab-muted)", margin: "5px 0 0", lineHeight: 1.5, whiteSpace: "pre-wrap" }}>
                {citation.text}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
