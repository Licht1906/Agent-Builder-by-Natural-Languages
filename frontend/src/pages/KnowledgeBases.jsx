import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft, Library, Plus, Trash2, Pencil, Upload, FileText, RefreshCw,
  AlertTriangle, Check, X, Eye, Database, Plug, ShieldAlert,
} from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import ConfirmDialog from "../components/ConfirmDialog";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   Knowledge Bases: the shelf-management screen (RAG-13 / #99), the mode
   picker that decides where a new shelf's contents live (RAG-22 / #108), and,
   one shelf at a time, the document upload + processing-status screen
   (RAG-12 / #98).

   A managed shelf's screens never say "vector", "embedding", "chunk" or
   "Qdrant" — a creator reads pipeline stages, file names and counts, never
   the machinery behind them.

   The BYODB half is the one deliberate exception: somebody wiring their own
   vector database up *is* the person who knows what a collection and an
   embedding dimension are, and hiding those words from them would leave them
   guessing which of their own fields we meant. What is still never shown
   raw there is a *failure* — see `CONNECTION_FAILURE`.
   ============================================================ */

//: Mirrors `src.workflow.documents.MAX_DOCUMENT_BYTES` and `SUPPORTED_SUFFIXES`
//: — stated here too so the limit is on screen *before* a creator picks a
//: file, not discovered from a refusal after the fact.
const MAX_UPLOAD_MB = 20;
const SUPPORTED_EXTENSIONS = [".md", ".txt", ".pdf", ".docx"];

const STATUS_LABEL = {
  pending: "Đang chờ xử lý…",
  extracting: "Đang đọc tài liệu…",
  chunking: "Đang chia đoạn…",
  embedding: "Đang lập chỉ mục…",
  indexed: "Sẵn sàng tra cứu",
  failed: "Không xử lý được",
};

const IN_PROGRESS_STATUSES = new Set(["pending", "extracting", "chunking", "embedding"]);

/** A technical refusal, reworded with something to actually do about it —
 *  never a stack trace or a raw status code (RAG-12's own acceptance line). */
/** A shelf name guessed from a file's own name — RAG-13 (#99)'s "gợi ý tên tự
 *  động từ tài liệu đầu tiên, sửa tay được": a suggestion `KnowledgeBaseDetail`
 *  offers to accept, never applies on its own, and never overwrites a name
 *  the creator already chose for anything but a brand-new, still-empty shelf. */
function suggestedNameFrom(fileName) {
  const stem = fileName.replace(/\.[^.]+$/, "");
  const words = stem.replace(/[-_]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

function friendlyUploadError(message) {
  if (/Chỉ hỗ trợ/.test(message)) return `${message}. Thử lưu lại thành .docx hoặc .pdf rồi tải lên lại.`;
  if (/vượt quá/.test(message)) return `${message}. Thử chia nhỏ tài liệu hoặc nén bớt hình ảnh trước khi tải lên.`;
  if (/rỗng/.test(message)) return `${message}. Kiểm tra lại tệp trước khi tải lên.`;
  return message;
}

/* ============================================================
   BYODB (RAG-22 / #108): the two modes, and how a failed connection test is
   put into words.
   ============================================================ */

//: The `/test` verdict, said in a sentence a creator can act on, keyed by
//: `ExternalConnectionResponse.status_reason` — the backend's own name for
//: which *kind* of failure this was. Keyed on the code and never on
//: `status_detail`'s prose: that sentence carries a URL, an httpx exception's
//: own words, or the first 200 bytes the far end answered with, and matching
//: on it would make this screen a second parser for something the probe
//: already knows the parts of. `detail` is still shown, folded away, because
//: whoever set the database up is also the person it eventually helps.
const CONNECTION_FAILURE = {
  provider_unsupported: {
    what: "Loại database này chưa kiểm tra tự động được",
    fix: "Bản này mới kiểm tra được Qdrant. Bạn vẫn lưu được kết nối, nhưng chưa có cách xác nhận nó chạy.",
  },
  blocked_address: {
    what: "Địa chỉ này không được phép gọi tới",
    fix: "Hệ thống chỉ gọi ra địa chỉ công khai. Địa chỉ nội bộ — localhost, 127.0.0.1, 169.254.x.x hay dải mạng LAN — bị chặn.",
  },
  unreachable: {
    what: "Không gọi tới được máy chủ",
    fix: "Kiểm tra database còn chạy, cổng có mở ra Internet, và địa chỉ có gõ đúng không.",
  },
  redirect_refused: {
    what: "Máy chủ chuyển hướng sang một địa chỉ khác",
    fix: "Vì an toàn, hệ thống không đi theo chuyển hướng. Hãy điền thẳng địa chỉ cuối cùng.",
  },
  auth_rejected: {
    what: "Database từ chối thông tin đăng nhập",
    fix: "Kiểm tra API key đang lưu dưới khoá bí mật này còn đúng và còn hạn.",
  },
  credential_missing: {
    what: "Chưa có API key nào được lưu dưới khoá bí mật này",
    fix: "Nhờ quản trị viên lưu giá trị cho khoá đó, hoặc để trống nếu database của bạn không cần key.",
  },
  credential_unreadable: {
    what: "Không đọc được API key đã lưu",
    fix: "Nhờ quản trị viên lưu lại giá trị cho khoá bí mật này.",
  },
  collection_not_found: {
    what: "Không tìm thấy collection trên database",
    fix: "Kiểm tra lại tên collection — tên có phân biệt chữ hoa chữ thường.",
  },
  server_error: {
    what: "Database trả về lỗi",
    fix: "Thử lại sau ít phút, hoặc xem log ở phía database của bạn xem nó báo gì.",
  },
  unreadable_answer: {
    what: "Không đọc được cấu hình vector của collection",
    fix:
      "Kiểm tra địa chỉ có đúng là API của Qdrant (thường là cổng 6333) không. " +
      "Nếu collection của bạn có nhiều vector được đặt tên, điền tên vector cần dùng ở phần tên trường bên dưới.",
  },
  dimension_mismatch: {
    what: "Số chiều vector khai báo không khớp với collection",
    fix: "Sửa ô “Số chiều vector” cho đúng với collection, hoặc trỏ sang collection được tạo bằng đúng model nhúng bạn dùng.",
  },
};

const UNKNOWN_FAILURE = {
  what: "Kết nối không thành công",
  fix: "Kiểm tra lại địa chỉ, tên collection và API key, rồi thử lại.",
};

const PROVIDERS = [
  { value: "qdrant", label: "Qdrant" },
  { value: "pinecone", label: "Pinecone (chưa kiểm tra được)" },
  { value: "weaviate", label: "Weaviate (chưa kiểm tra được)" },
  { value: "pgvector", label: "pgvector (chưa kiểm tra được)" },
];

const DISTANCE_METRICS = [
  { value: "cosine", label: "Cosine" },
  { value: "euclidean", label: "Euclidean" },
  { value: "dot", label: "Dot product" },
];

//: The logical fields `src.services.vector_adapter.DEFAULT_FIELD_NAMES` reads,
//: with the payload key each falls back to. Shown as five optional inputs
//: rather than a JSON box: a creator whose collection stores its text under
//: `content` should not have to know this is a dict on the wire.
const FIELD_MAPPING_FIELDS = [
  { key: "text_field", label: "Trường chứa nội dung", fallback: "chunk_text" },
  { key: "doc_name_field", label: "Trường tên tài liệu", fallback: "doc_name" },
  { key: "section_path_field", label: "Trường mục/chương", fallback: "section_path" },
  { key: "page_number_field", label: "Trường số trang", fallback: "page_number" },
  { key: "vector_field", label: "Tên vector (nếu collection đặt tên)", fallback: "vector mặc định" },
];

const EMPTY_CONNECTION_FORM = {
  name: "",
  provider: "qdrant",
  connection_url: "",
  secret_ref: "",
  collection_name: "",
  embedding_dimension: "1024",
  distance_metric: "cosine",
  field_mapping: {},
};

/** Does this shelf read from a database this platform does not run?
 *
 *  One reading of `KnowledgeBaseMode` for every screen that branches on it —
 *  which icon, which subtitle, whether an upload surface is offered at all.
 *  Spelled as a question about the shelf rather than as `kb.mode === "byodb"`
 *  repeated at each site, so the enum's spelling lives in one place. */
const isExternal = (kb) => kb.mode === "byodb";

/** The connection form as the API wants it, or `null` if a required box is still empty. */
function connectionPayloadOf(form) {
  const dimension = Number.parseInt(form.embedding_dimension, 10);
  if (!form.name.trim() || !form.connection_url.trim() || !form.collection_name.trim()) return null;
  if (!Number.isInteger(dimension) || dimension <= 0) return null;
  const mapping = {};
  for (const [key, value] of Object.entries(form.field_mapping)) {
    if (value && value.trim()) mapping[key] = value.trim();
  }
  return {
    name: form.name.trim(),
    provider: form.provider,
    connection_url: form.connection_url.trim(),
    secret_ref: form.secret_ref.trim(),
    collection_name: form.collection_name.trim(),
    embedding_dimension: dimension,
    distance_metric: form.distance_metric,
    field_mapping: mapping,
  };
}

export default function KnowledgeBasesPage({ notify }) {
  const [openKbId, setOpenKbId] = useState(null);

  if (openKbId) {
    return <KnowledgeBaseDetail kbId={openKbId} back={() => setOpenKbId(null)} notify={notify} />;
  }
  return <KnowledgeBaseList notify={notify} openKb={setOpenKbId} />;
}

// --------------------------------------------------------------------------- //
// The list screen: every shelf, how much is on it, who is using it
// --------------------------------------------------------------------------- //

function KnowledgeBaseList({ notify, openKb }) {
  const [shelves, setShelves] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [composing, setComposing] = useState(false); // the mode picker + create form (RAG-22)
  const [renaming, setRenaming] = useState(null); // { id, name }
  const [doomed, setDoomed] = useState(null); // { kb, message, affectedAgents, blocking }
  const [deleteBusy, setDeleteBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setShelves(await api.knowledgeBases.list());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function afterCreate(name) {
    setComposing(false);
    notify(`Đã tạo “${name}”`);
    await load();
  }

  async function commitRename() {
    if (!renaming) return;
    const name = renaming.name.trim();
    const target = shelves.find((kb) => kb.id === renaming.id);
    setRenaming(null);
    if (!name || !target || name === target.name) return;
    try {
      await api.knowledgeBases.rename(renaming.id, name);
      await load();
    } catch (err) {
      notify(err.message, "error");
    }
  }

  /** First call with no confirmation — a 409 names who is affected, which is
   *  exactly what the dialog needs before a caller decides to proceed. */
  async function requestDelete(kb) {
    try {
      await api.knowledgeBases.remove(kb.id, false);
      notify(`Đã xoá “${kb.name}”`);
      await load();
    } catch (err) {
      const detail = err.body && err.body.detail;
      if (err.status === 409 && detail && typeof detail === "object") {
        setDoomed({ kb, message: detail.message, affectedAgents: detail.affected_agents || [], blocking: !!detail.blocking });
        return;
      }
      notify(err.message, "error");
    }
  }

  async function confirmDelete() {
    if (!doomed || doomed.blocking) { setDoomed(null); return; }
    setDeleteBusy(true);
    try {
      await api.knowledgeBases.remove(doomed.kb.id, true);
      notify(`Đã xoá “${doomed.kb.name}”`);
      setDoomed(null);
      await load();
    } catch (err) {
      notify(err.message, "error");
      setDoomed(null);
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <PageShell width="narrow">
        <PageHead title="Knowledge Bases" hint="Where a Knowledge Search step reads from. Add documents to a shelf without ever touching a workflow.">
          {!composing && (
            <button type="button" className="btn btn-primary" onClick={() => setComposing(true)}>
              <Plus size={14} />Tạo Knowledge Base
            </button>
          )}
        </PageHead>

        {composing && <CreateKnowledgeBase onCancel={() => setComposing(false)} onCreated={afterCreate} notify={notify} />}

        <ErrorNote>{error}</ErrorNote>

        {loading && <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}><AgentMascot state="exploring" size={78} /></div>}

        {!loading && shelves.length === 0 && (
          <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>No Knowledge Base yet — create one above, then add documents to it.</div>
        )}

        <div className="ab-stack">
          {shelves.map((kb) => (
            <div key={kb.id} className="card" style={{ flexDirection: "row", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
              <ShelfIcon kb={kb} />

              <div style={{ flex: "1 1 220px", minWidth: 0 }}>
                {renaming && renaming.id === kb.id ? (
                  <input
                    autoFocus
                    className="input"
                    style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}
                    value={renaming.name}
                    onChange={(event) => setRenaming({ id: kb.id, name: event.target.value })}
                    onBlur={commitRename}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") event.currentTarget.blur();
                      if (event.key === "Escape") setRenaming(null);
                    }}
                  />
                ) : (
                  <button
                    type="button"
                    className="btn btn-ghost card-title"
                    style={{ padding: "0 var(--space-1)", border: 0, color: "var(--color-text)" }}
                    onClick={() => openKb(kb.id)}
                    title="Open this shelf"
                  >
                    {kb.name}
                  </button>
                )}
                <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "2px 0 0" }}>
                  {isExternal(kb) ? kb.external_connection_name || "chưa gắn kết nối" : `${kb.document_count} tài liệu`}
                  {kb.agent_names.length > 0 && ` · dùng bởi ${kb.agent_names.join(", ")}`}
                </p>
              </div>

              <ModeBadge kb={kb} />

              <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => openKb(kb.id)}>Open</button>
                <button type="button" className="btn btn-ghost btn-icon" title="Rename" onClick={() => setRenaming({ id: kb.id, name: kb.name })}>
                  <Pencil size={14} />
                </button>
                <button type="button" className="btn btn-ghost btn-icon" title="Delete" onClick={() => requestDelete(kb)}>
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>

      <ConfirmDialog
        open={!!doomed}
        title={doomed && doomed.blocking ? "Không thể xoá" : `Xoá “${doomed && doomed.kb.name}”?`}
        body={
          doomed && (
            <div>
              <p style={{ margin: 0 }}>{doomed.message}</p>
              {doomed.affectedAgents.length > 0 && (
                <ul style={{ margin: "10px 0 0", paddingLeft: "var(--space-4)" }}>
                  {doomed.affectedAgents.map((name) => <li key={name}>{name}</li>)}
                </ul>
              )}
            </div>
          )
        }
        confirmLabel={doomed && doomed.blocking ? "Đã hiểu" : "Xoá"}
        busy={deleteBusy}
        onConfirm={confirmDelete}
        onCancel={() => setDoomed(null)}
      />
    </PageShell>
  );
}

// --------------------------------------------------------------------------- //
// Telling the two kinds of shelf apart, at a glance (RAG-22 / #108)
// --------------------------------------------------------------------------- //

/* The teal-and-sky pair this used to paint came from a palette the design
   system no longer has; on the current one it was simply two off-brand
   colours. The two modes are still told apart — by the glyph, which is the
   part that carries the meaning — inside the same square accent frame every
   other card icon in the app uses. */
function ShelfIcon({ kb }) {
  return (
    <span style={{ width: 32, height: 32, flexShrink: 0, display: "grid", placeItems: "center", border: "1px solid var(--color-divider)", color: "var(--color-accent)" }}>
      {isExternal(kb) ? <Database size={16} /> : <Library size={16} />}
    </span>
  );
}

/** "Tự quản lý" / "Kết nối ngoài" — the ticket's own "badge phân biệt" on the list.
 *
 *  A BYODB shelf also carries the last `/test` verdict, because "connected" is
 *  the only thing that makes the badge mean anything: a shelf pointed at a
 *  database nobody has reached yet looks identical to a working one otherwise. */
function ModeBadge({ kb }) {
  if (!isExternal(kb)) return <span className="tag tag-accent">Tự quản lý</span>;
  /* `rgba(255,255,255,.04)` was the old background here, which is invisible on
     a light ground — the badge had no fill at all in light mode. The tag
     variants carry their own, and each verdict now picks the one that says
     what it means. */
  const verdict = {
    connected: { cls: "tag tag-accent", label: "Kết nối ngoài" },
    failed: { cls: "tag tag-danger", label: "Kết nối ngoài · lỗi" },
    untested: { cls: "tag tag-warn", label: "Kết nối ngoài · chưa kiểm tra" },
  }[kb.external_connection_status] || { cls: "tag tag-neutral", label: "Kết nối ngoài" };
  return <span className={verdict.cls}>{verdict.label}</span>;
}

// --------------------------------------------------------------------------- //
// Creating a shelf: which of the two modes, and — for BYODB — where from
// --------------------------------------------------------------------------- //

/** The mode picker (RAG-22 / #108): two cards, never a dropdown.
 *
 *  A dropdown would make this look like a setting with a sensible default
 *  somebody can skip past. It is not — it decides whether this platform holds
 *  the creator's documents or merely reads someone else's database, and the
 *  two sentences on the cards are the only place a non-technical creator is
 *  told the difference. So both options are on screen at once, in full, with
 *  the consequences of the advanced one spelled out before it can be picked. */
function CreateKnowledgeBase({ onCancel, onCreated, notify }) {
  const [mode, setMode] = useState("managed");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  //: The `ExternalConnection` "Kiểm tra kết nối" created, and whether it still
  //: matches what is typed in the form. One piece of state, not two, because
  //: a row is only ever meaningful together with that answer: editing a field
  //: puts `verified` back to false while keeping `row` (there is no update
  //: endpoint — the next test replaces it), and a verdict left standing beside
  //: changed settings would be a green tick for a connection nobody tried.
  const [draft, setDraft] = useState({ row: null, verified: false });
  const [acknowledged, setAcknowledged] = useState(false);

  /** A draft row nobody adopted is an orphan — nothing points at it and no
   *  screen lists it, so whoever abandons it has to clean it up. A failure
   *  here is silent on purpose: the creator asked to close a form or switch
   *  modes, not to hear about our bookkeeping. */
  async function discardDraft() {
    if (draft.row) await api.externalConnections.remove(draft.row.id).catch(() => {});
    setDraft({ row: null, verified: false });
    setAcknowledged(false);
  }

  /** Switching modes throws the draft away rather than parking it.
   *
   *  `ExternalConnectionSetup` unmounts on the way to `managed`, taking the
   *  typed-in form with it — a draft kept across that would come back as a
   *  green "Kết nối thành công" sitting beside boxes the creator can see are
   *  empty. Discarding is also what keeps a shelf created in `managed` mode
   *  from leaving a tested-but-unused connection row behind. */
  async function chooseMode(next) {
    if (next === mode) return;
    setMode(next);
    if (next === "managed") await discardDraft();
  }

  async function cancel() {
    await discardDraft();
    onCancel();
  }

  async function submit(event) {
    event.preventDefault();
    const shelfName = name.trim();
    if (!shelfName) return;
    setBusy(true);
    try {
      await api.knowledgeBases.create(shelfName, "", mode === "byodb" ? draft.row.id : null);
      await onCreated(shelfName);
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  const ready = mode === "managed" ? !!name.trim() : !!name.trim() && draft.verified && acknowledged;

  return (
    <form onSubmit={submit} className="card" style={{ gap: 18, marginBottom: 24 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
        <ModeCard
          selected={mode === "managed"}
          onSelect={() => chooseMode("managed")}
          icon={<Library size={17} />}
          title="Để hệ thống tự quản lý"
          tagline="Chỉ cần upload file, chúng tôi lo phần còn lại"
          points={["Tải tài liệu lên, hệ thống tự đọc và lập chỉ mục", "Tìm kiếm đầy đủ: vừa theo nghĩa, vừa theo từ khoá"]}
        />
        <ModeCard
          selected={mode === "byodb"}
          onSelect={() => chooseMode("byodb")}
          icon={<Database size={17} />}
          title="Kết nối tới database riêng của bạn"
          advanced
          tagline="Dành cho đội kỹ thuật đã có sẵn hạ tầng dữ liệu"
          points={["Dữ liệu ở nguyên chỗ của bạn — hệ thống chỉ đọc để trả lời", "Tìm kiếm theo nghĩa, không có tìm theo từ khoá"]}
        />
      </div>

      <div className="field">
        <label>Tên tủ tài liệu</label>
        <input
          autoFocus
          className="input"
          placeholder="Ví dụ: Quy chế nhân sự"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </div>

      {mode === "byodb" && (
        <ExternalConnectionSetup
          draft={draft}
          onTested={(row) => setDraft({ row, verified: true })}
          onEdited={() => { setDraft((current) => ({ ...current, verified: false })); setAcknowledged(false); }}
          acknowledged={acknowledged}
          onAcknowledge={setAcknowledged}
          notify={notify}
        />
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
        <button type="button" className="btn btn-secondary" onClick={cancel}>Huỷ</button>
        <button className="btn btn-primary" type="submit" disabled={busy || !ready}>
          {busy ? <Spinner size={13} /> : <Plus size={14} />}
          {mode === "byodb" && draft.verified && draft.row.status !== "connected" ? "Tạo dù chưa kết nối được" : "Tạo"}
        </button>
      </div>
    </form>
  );
}

function ModeCard({ selected, onSelect, icon, title, tagline, points, advanced }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className="card"
      style={{
        textAlign: "left", cursor: "pointer", width: "100%", gap: 8,
        fontFamily: "var(--font-body)", fontWeight: 400, color: "var(--color-text)",
        borderColor: selected ? "var(--color-accent)" : "var(--color-divider)",
        background: selected ? "color-mix(in srgb, var(--color-accent) 7%, transparent)" : "transparent",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
        <span style={{ display: "flex", color: selected ? "var(--color-accent)" : "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>{icon}</span>
        <span className="card-title">{title}</span>
        {advanced && <span className="tag tag-neutral">Nâng cao</span>}
        {selected && <Check size={15} style={{ color: "var(--color-accent)", marginLeft: "auto" }} />}
      </div>
      <p style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.5 }}>{tagline}</p>
      <ul className="text-muted" style={{ margin: 0, paddingLeft: "var(--space-4)", display: "flex", flexDirection: "column", gap: 4 }}>
        {points.map((point) => (
          <li key={point} style={{ fontSize: "var(--text-sm)", lineHeight: 1.5 }}>{point}</li>
        ))}
      </ul>
    </button>
  );
}

/** Everything the BYODB half of the picker asks for, plus the two warnings the
 *  ticket requires a creator to have read before confirming.
 *
 *  "Kiểm tra kết nối" is what actually creates the `ExternalConnection` row:
 *  `/test` runs against a stored row (it is rate-limited and SSRF-guarded per
 *  owner), so there is nothing to test until one exists. Editing any field
 *  afterwards replaces that row rather than mutating it — connections have no
 *  update endpoint, and a verdict left standing next to changed settings would
 *  be a green tick for a connection nobody tried. */
function ExternalConnectionSetup({ draft, onTested, onEdited, acknowledged, onAcknowledge, notify }) {
  const [form, setForm] = useState(EMPTY_CONNECTION_FORM);
  const [testing, setTesting] = useState(false);

  const payload = connectionPayloadOf(form);
  //: A row exists but no longer describes what is on screen. Derived from the
  //: draft rather than tracked separately — `verified` is already the one
  //: answer to "does the row match the form", and a second copy of it here
  //: could only ever drift from the first.
  const stale = draft.row !== null && !draft.verified;

  function edited(next) {
    setForm(next);
    onEdited();
  }

  const edit = (field, value) => edited({ ...form, [field]: value });
  const editMapping = (key, value) => edited({ ...form, field_mapping: { ...form.field_mapping, [key]: value } });

  async function testConnection() {
    if (!payload) return;
    setTesting(true);
    try {
      // The previous draft row goes first: connections have no update
      // endpoint, so "test again after an edit" means a new row, and the one
      // it replaces would otherwise be left behind pointing at nothing.
      if (draft.row) await api.externalConnections.remove(draft.row.id).catch(() => {});
      const created = await api.externalConnections.create(payload);
      onTested(await api.externalConnections.test(created.id));
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setTesting(false);
    }
  }

  return (
    <div style={{ borderTop: "1px solid var(--color-divider)", paddingTop: "var(--space-4)" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 12 }}>
        <Field label="Tên kết nối">
          <input className="input" placeholder="Qdrant của phòng dữ liệu" value={form.name} onChange={(e) => edit("name", e.target.value)} />
        </Field>
        <Field label="Loại database">
          <select className="input" value={form.provider} onChange={(e) => edit("provider", e.target.value)}>
            {PROVIDERS.map((provider) => <option key={provider.value} value={provider.value}>{provider.label}</option>)}
          </select>
        </Field>
        <Field label="Địa chỉ kết nối" hint="Phải là địa chỉ công khai — địa chỉ nội bộ bị chặn.">
          <input className="input" placeholder="https://qdrant.congty.vn:6333" value={form.connection_url} onChange={(e) => edit("connection_url", e.target.value)} />
        </Field>
        <Field label="Khoá bí mật chứa API key" hint="Tên khoá trong kho bí mật, không phải chính API key. Để trống nếu database không cần key.">
          <input className="input" placeholder="ext/qdrant-phong-du-lieu" value={form.secret_ref} onChange={(e) => edit("secret_ref", e.target.value)} />
        </Field>
        <Field label="Tên collection">
          <input className="input" placeholder="tai_lieu_noi_bo" value={form.collection_name} onChange={(e) => edit("collection_name", e.target.value)} />
        </Field>
        <Field label="Số chiều vector" hint="Do model nhúng bạn dùng quyết định. Sai số chiều thì bước kiểm tra sẽ báo.">
          <input className="input" inputMode="numeric" value={form.embedding_dimension} onChange={(e) => edit("embedding_dimension", e.target.value)} />
        </Field>
        <Field label="Cách đo khoảng cách">
          <select className="input" value={form.distance_metric} onChange={(e) => edit("distance_metric", e.target.value)}>
            {DISTANCE_METRICS.map((metric) => <option key={metric.value} value={metric.value}>{metric.label}</option>)}
          </select>
        </Field>
      </div>

      <details style={{ marginTop: 14 }}>
        <summary className="text-muted" style={{ fontSize: "var(--text-sm)", cursor: "pointer" }}>
          Tên trường trong collection của bạn (chỉ điền nếu khác mặc định)
        </summary>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12, marginTop: 12 }}>
          {FIELD_MAPPING_FIELDS.map((field) => (
            <Field key={field.key} label={field.label}>
              <input
                className="input"
                placeholder={field.fallback}
                value={form.field_mapping[field.key] || ""}
                onChange={(e) => editMapping(field.key, e.target.value)}
              />
            </Field>
          ))}
        </div>
      </details>

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 14, flexWrap: "wrap" }}>
        <button type="button" className="btn btn-secondary btn-sm" onClick={testConnection} disabled={testing || !payload}>
          {testing ? <Spinner size={13} /> : <Plug size={14} />}Kiểm tra kết nối
        </button>
        {!payload && <span className="text-muted" style={{ fontSize: "var(--text-sm)" }}>Điền tên, địa chỉ, collection và số chiều trước đã.</span>}
        {stale && <span className="tag tag-warn">Cấu hình đã đổi — kiểm tra lại.</span>}
      </div>

      {draft.verified && <ConnectionVerdict connection={draft.row} />}

      <ExternalDataWarnings />

      <label style={{ display: "flex", alignItems: "flex-start", gap: 9, marginTop: 14, fontSize: "var(--text-sm)", lineHeight: 1.55, cursor: "pointer" }}>
        <input type="checkbox" checked={acknowledged} onChange={(e) => onAcknowledge(e.target.checked)} style={{ marginTop: 2 }} />
        <span>Tôi hiểu tủ này chỉ tìm theo nghĩa (không có tìm theo từ khoá), và hệ thống không index hay sửa dữ liệu trong database của tôi.</span>
      </label>
    </div>
  );
}

function Field({ label, hint, children }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: "5px 0 0", lineHeight: 1.5 }}>{hint}</p>}
    </div>
  );
}

/** The `/test` answer, said in words — never the probe's own sentence on its own.
 *
 *  `status_reason` is the backend's name for which kind of failure this was;
 *  `status_detail` is the technical sentence behind it, folded away rather
 *  than dropped, because the person who set the database up is eventually the
 *  one who needs the URL and the HTTP status. */
function ConnectionVerdict({ connection }) {
  if (connection.status === "connected") {
    return (
      <p style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: "var(--text-sm)", color: "var(--color-accent)", margin: "12px 0 0", lineHeight: 1.55 }}>
        <Check size={15} style={{ flexShrink: 0, marginTop: 2 }} />
        Kết nối thành công — collection đọc được và số chiều khớp với khai báo.
      </p>
    );
  }
  const failure = CONNECTION_FAILURE[connection.status_reason] || UNKNOWN_FAILURE;
  return (
    /* `#f8a5a5` is a dark-mode text colour; on the light ground this screen
       actually opens on it was near-invisible against its own tint. */
    <div style={{ margin: "12px 0 0", padding: "var(--space-2) var(--space-3)", border: "1px solid color-mix(in srgb, #ef4444 35%, transparent)", background: "color-mix(in srgb, #ef4444 7%, transparent)" }}>
      <p style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: "var(--text-sm)", color: "#a12626", margin: 0, lineHeight: 1.55 }}>
        <X size={15} style={{ flexShrink: 0, marginTop: 2 }} />
        <span><strong>{failure.what}.</strong> {failure.fix}</span>
      </p>
      {connection.status_detail && (
        <details style={{ marginTop: 8 }}>
          <summary className="text-muted" style={{ fontSize: "var(--text-xs)", cursor: "pointer" }}>Chi tiết kỹ thuật</summary>
          <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "6px 0 0", lineHeight: 1.5, wordBreak: "break-word" }}>
            {connection.status_detail}
          </p>
        </details>
      )}
    </div>
  );
}

/** The two things a creator has to know before choosing BYODB, said outright.
 *
 *  Both are on the ticket's own checklist, and both are the kind of thing that
 *  is only ever discovered later otherwise: that this platform will not touch
 *  the data (so nothing here can fix a badly-structured collection), and that
 *  search on this shelf is dense-only (so a query that would have been found
 *  by an exact term on a managed shelf may not be found here). */
function ExternalDataWarnings() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 14 }}>
      <p style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: "var(--text-sm)", color: "#8a5a06", margin: 0, lineHeight: 1.55 }}>
        <ShieldAlert size={15} style={{ flexShrink: 0, marginTop: 2 }} />
        <span>
          Hệ thống <strong>không index và không sửa</strong> dữ liệu trong database của bạn — chỉ đọc để trả lời.
          Cấu trúc dữ liệu, chất lượng vector và quyền truy cập ở đó do bạn tự chịu trách nhiệm.
        </span>
      </p>
      <p style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: "var(--text-sm)", color: "#8a5a06", margin: 0, lineHeight: 1.55 }}>
        <AlertTriangle size={15} style={{ flexShrink: 0, marginTop: 2 }} />
        <span>
          Tủ kết nối ngoài chỉ tìm <strong>theo nghĩa</strong> (dense-only). Tủ do hệ thống tự quản lý còn tìm thêm
          theo từ khoá rồi trộn hai kết quả lại, nên nó bắt được mã số, tên riêng và thuật ngữ hiếm tốt hơn.
        </span>
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// One shelf: upload, live processing status, and what is already on it
// --------------------------------------------------------------------------- //

function KnowledgeBaseDetail({ kbId, back, notify }) {
  const [kb, setKb] = useState(null);
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(0);
  const [removing, setRemoving] = useState(null); // document id pending confirm
  const [nameSuggestion, setNameSuggestion] = useState(null);
  const fileInput = useRef(null);

  const load = useCallback(async () => {
    try {
      const [kbRow, docRows] = await Promise.all([api.knowledgeBases.read(kbId), api.documents.list(kbId)]);
      setKb(kbRow);
      setDocs(docRows);
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => { load(); }, [load]);

  // Poll while anything is still moving through the pipeline — the whole
  // reason RAG-12 exists is watching `pending → … → indexed` without a
  // manual refresh. Stops on its own the moment nothing is in progress.
  useEffect(() => {
    if (!docs.some((doc) => IN_PROGRESS_STATUSES.has(doc.status))) return undefined;
    const timer = setInterval(() => {
      api.documents.list(kbId).then(setDocs).catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, [docs, kbId]);

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;
    // "Tài liệu đầu tiên" (RAG-13 / #99): true only when this shelf had no
    // documents before this call — captured now, since `docs` moves under us
    // as each upload lands and `load()` refetches it below.
    const isFirstDocument = docs.length === 0;
    setUploading((n) => n + files.length);
    let uploadedAny = false;
    for (const file of files) {
      try {
        await api.documents.upload(kbId, file);
        uploadedAny = true;
      } catch (err) {
        notify(friendlyUploadError(err.message), "error");
      } finally {
        setUploading((n) => n - 1);
      }
    }
    if (isFirstDocument && uploadedAny) {
      const suggested = suggestedNameFrom(files[0].name);
      if (suggested && suggested !== kb.name) setNameSuggestion(suggested);
    }
    await load();
  }

  async function acceptNameSuggestion() {
    const suggested = nameSuggestion;
    setNameSuggestion(null);
    try {
      await api.knowledgeBases.rename(kbId, suggested);
      await load();
    } catch (err) {
      notify(err.message, "error");
    }
  }

  function onDrop(event) {
    event.preventDefault();
    setDragOver(false);
    uploadFiles(event.dataTransfer.files);
  }

  async function reingest(doc) {
    try {
      await api.documents.reingest(kbId, doc.id);
      await load();
    } catch (err) {
      notify(err.message, "error");
    }
  }

  async function remove(doc) {
    setRemoving(null);
    try {
      await api.documents.remove(kbId, doc.id);
      await load();
    } catch (err) {
      notify(err.message, "error");
    }
  }

  async function view(doc) {
    try {
      const objectUrl = await api.documents.viewContent(kbId, doc.id);
      window.open(objectUrl, "_blank", "noopener,noreferrer");
    } catch (err) {
      notify(err.message, "error");
    }
  }

  return (
    <PageShell width="narrow">
        <PageHead title={kb ? kb.name : "…"}>
          <button type="button" className="btn btn-secondary" onClick={back}>
            <ArrowLeft size={14} />Knowledge Bases
          </button>
        </PageHead>
        {kb && (
          <div className="ab-row" style={{ marginBottom: "var(--space-4)" }}>
            <ModeBadge kb={kb} />
            <span className="text-muted" style={{ fontSize: "var(--text-xs)" }}>
              {isExternal(kb) ? "Đọc từ database riêng của bạn" : `${docs.length} tài liệu`}
              {kb.agent_names.length > 0 ? ` · dùng bởi ${kb.agent_names.join(", ")}` : ""}
            </span>
          </div>
        )}

        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}
        <ErrorNote>{error}</ErrorNote>

        {kb && (
          <>

            {isExternal(kb) && <ExternalShelf kb={kb} notify={notify} />}

            {!isExternal(kb) && nameSuggestion && (
              <div className="card card-ok" style={{ flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
                <Pencil size={14} style={{ color: "var(--color-accent)", flexShrink: 0 }} />
                <span style={{ fontSize: "var(--text-sm)", flex: 1 }}>Đổi tên tủ thành “{nameSuggestion}”?</span>
                <button type="button" className="btn btn-primary btn-sm" onClick={acceptNameSuggestion}>Đổi tên</button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setNameSuggestion(null)}>Bỏ qua</button>
              </div>
            )}

            {/* No upload surface on a BYODB shelf — the API refuses one
                (`store_document`), and offering a dropzone that always fails
                would contradict the very warning this screen just showed. */}
            {!isExternal(kb) && (
              <>
                <div
                  onDragOver={(event) => { event.preventDefault(); setDragOver(true); }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={onDrop}
                  style={{
                    padding: "var(--space-10) var(--space-6)", textAlign: "center", cursor: "pointer",
                    border: "1px dashed", transition: "border-color .15s, background .15s",
                    borderColor: dragOver ? "var(--color-accent)" : "var(--color-divider)",
                    background: dragOver ? "color-mix(in srgb, var(--color-accent) 6%, transparent)" : "transparent",
                  }}
                  onClick={() => fileInput.current?.click()}
                >
                  <Upload size={24} style={{ color: "color-mix(in srgb, var(--color-text) 52%, transparent)", marginBottom: 10 }} />
                  <p style={{ margin: "0 0 4px", fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>Kéo thả tệp vào đây, hoặc bấm để chọn</p>
                  <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: 0 }}>
                    Định dạng: {SUPPORTED_EXTENSIONS.join(", ")} · Tối đa {MAX_UPLOAD_MB} MB mỗi tệp
                  </p>
                  <input
                    ref={fileInput}
                    type="file"
                    multiple
                    accept={SUPPORTED_EXTENSIONS.join(",")}
                    style={{ display: "none" }}
                    onChange={(event) => { uploadFiles(event.target.files); event.target.value = ""; }}
                  />
                </div>
                {uploading > 0 && (
                  <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "8px 0 0", display: "flex", alignItems: "center", gap: 6 }}>
                    <Spinner size={13} />Đang tải lên {uploading} tệp…
                  </p>
                )}

                <div className="ab-stack" style={{ gap: 10, marginTop: 22 }}>
                  {docs.length === 0 && !loading && (
                    <p className="text-muted">Chưa có tài liệu nào trên tủ này.</p>
                  )}
                  {docs.map((doc) => (
                    <DocumentRow key={doc.id} doc={doc} onView={() => view(doc)} onReingest={() => reingest(doc)} onRemove={() => setRemoving(doc)} />
                  ))}
                </div>
              </>
            )}
          </>
        )}

      <ConfirmDialog
        open={!!removing}
        title={removing && `Xoá “${removing.file_name}”?`}
        body="Tài liệu và chỉ mục tìm kiếm của nó sẽ bị xoá. Không thể hoàn tác."
        confirmLabel="Xoá"
        onConfirm={() => remove(removing)}
        onCancel={() => setRemoving(null)}
      />
    </PageShell>
  );
}

/** A BYODB shelf's own screen: where it reads from, whether that still works,
 *  and the same two warnings the create form made before it existed.
 *
 *  The settings are read-only. A connection has no update endpoint by design
 *  (`ExternalConnection` is chosen once, at creation), and re-pointing a shelf
 *  at a different collection would change what an already-confirmed
 *  `KnowledgeSearch` node searches without the graph changing at all. What is
 *  offered instead is "Kiểm tra lại kết nối" — the same `/test`, run now. */
function ExternalShelf({ kb, notify }) {
  const [connection, setConnection] = useState(null);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!kb.external_connection_id) return;
    api.externalConnections.read(kb.external_connection_id).then(setConnection).catch((err) => setError(err.message));
  }, [kb.external_connection_id]);

  async function retest() {
    setTesting(true);
    try {
      setConnection(await api.externalConnections.test(connection.id));
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setTesting(false);
    }
  }

  if (!kb.external_connection_id) {
    return <ErrorNote>Tủ này ở chế độ kết nối ngoài nhưng chưa gắn kết nối nào — agent sẽ báo lỗi khi tìm.</ErrorNote>;
  }

  return (
    <div className="card">
      <ErrorNote>{error}</ErrorNote>
      {!connection && !error && <Spinner size={16} />}
      {connection && (
        <>
          <div className="ab-row" style={{ gap: 10 }}>
            <Database size={16} style={{ color: "var(--color-accent)" }} />
            <span className="card-title">{connection.name}</span>
            <button type="button" className="btn btn-secondary btn-sm" style={{ marginLeft: "auto" }} onClick={retest} disabled={testing}>
              {testing ? <Spinner size={13} /> : <RefreshCw size={13} />}Kiểm tra lại kết nối
            </button>
          </div>

          <dl style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12, margin: 0 }}>
            <Detail label="Loại database" value={connection.provider} />
            <Detail label="Địa chỉ" value={connection.connection_url} />
            <Detail label="Collection" value={connection.collection_name} />
            <Detail label="Số chiều vector" value={String(connection.embedding_dimension)} />
            <Detail label="Cách đo khoảng cách" value={connection.distance_metric} />
            <Detail label="API key" value={connection.has_secret ? "Đang dùng khoá bí mật đã lưu" : "Không dùng"} />
          </dl>

          {connection.status === "untested"
            ? <span className="tag tag-warn" style={{ alignSelf: "flex-start" }}>Chưa kiểm tra kết nối này lần nào.</span>
            : <ConnectionVerdict connection={connection} />}

          <ExternalDataWarnings />
        </>
      )}
    </div>
  );
}

function Detail({ label, value }) {
  return (
    <div>
      <dt className="ab-label" style={{ marginBottom: 4 }}>{label}</dt>
      <dd className="ab-mono" style={{ fontSize: "var(--text-sm)", margin: 0, wordBreak: "break-word" }}>{value}</dd>
    </div>
  );
}

function DocumentRow({ doc, onView, onReingest, onRemove }) {
  const inProgress = IN_PROGRESS_STATUSES.has(doc.status);
  const failed = doc.status === "failed";
  const indexed = doc.status === "indexed";

  return (
    <div className="card" style={{ gap: 8 }}>
      <div className="ab-row" style={{ gap: 10 }}>
        <FileText size={16} style={{ color: failed ? "#b32828" : "color-mix(in srgb, var(--color-text) 52%, transparent)", flexShrink: 0 }} />
        <span style={{ flex: "1 1 160px", minWidth: 0, fontSize: "var(--text-sm)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={doc.file_name}>
          {doc.file_name}
        </span>

        <StatusPill doc={doc} />

        <span style={{ display: "flex", gap: 2, flexShrink: 0 }}>
          <button type="button" className="btn btn-ghost btn-icon" title="Xem lại" onClick={onView}>
            <Eye size={14} />
          </button>
          {failed && (
            <button type="button" className="btn btn-ghost btn-icon" title="Ingest lại" onClick={onReingest}>
              <RefreshCw size={14} />
            </button>
          )}
          <button type="button" className="btn btn-ghost btn-icon" title="Xoá" onClick={onRemove}>
            <Trash2 size={14} />
          </button>
        </span>
      </div>

      {indexed && (
        <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: 0 }}>
          {doc.chunk_count} đoạn nhận diện được
        </p>
      )}

      {indexed && doc.low_confidence && (
        <p style={{ display: "flex", gap: 6, alignItems: "flex-start", fontSize: "var(--text-sm)", color: "#8a5a06", margin: 0, lineHeight: 1.5 }}>
          <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
          Một số phần trong tài liệu khó đọc, độ chính xác trích dẫn có thể giảm.
        </p>
      )}

      {failed && (
        <p style={{ fontSize: "var(--text-sm)", color: "#a12626", margin: 0, lineHeight: 1.6 }}>
          Không xử lý được tài liệu này. Thử ingest lại — nếu vẫn lỗi, thử chụp ảnh rõ hơn hoặc tải lên bản .docx/.txt thay thế.
        </p>
      )}

      {inProgress && (
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0 }}>Không cần tải lại trang — trạng thái tự cập nhật.</p>
      )}
    </div>
  );
}

function StatusPill({ doc }) {
  const label = STATUS_LABEL[doc.status] || doc.status;
  if (doc.status === "indexed") {
    return <span className="tag tag-accent"><Check size={12} />{label}</span>;
  }
  if (doc.status === "failed") {
    return <span className="tag tag-danger"><X size={12} />{label}</span>;
  }
  return <span className="tag tag-neutral"><Spinner size={11} />{label}</span>;
}
