import { useState } from "react";
import { Check, FlaskConical, HelpCircle } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell } from "../components/ui";
import { AgentBusy } from "../components/mascot";
import { useAgentActivity } from "../components/agent-presence";

/* ============================================================
   UC1-05/06 (#119, #120) — the Enterprise meta-agent's config form.

   One description in, one `AgentSpec` draft out (FR-10/FR-11), laid out on
   the same seven field groups spec.md §7 maps the spec onto. `clarifications`
   (FR-14) are never a side list: each one is matched to the group whose field
   it names and rendered right there, because a question buried under the form
   instead of beside the field it is about is a question a reviewer skims past.
   ============================================================ */

/** spec.md §7's table: UI group -> which `clarifications[].field` values belong
 *  to it. Order matters — it is the order groups render in, and a clarification
 *  matches the first group whose prefix is a match. */
const GROUPS = [
  { key: "audience_who", title: "Đối tượng sử dụng", prefixes: ["audience.roles", "audience.departments"] },
  { key: "audience_scope", title: "Phân quyền sử dụng", prefixes: ["audience.data_scope"] },
  { key: "capabilities", title: "Agent được làm gì", prefixes: ["capabilities"] },
  { key: "prohibitions", title: "Agent không được làm gì", prefixes: ["prohibitions"] },
  { key: "authority", title: "Quyền hạn của agent", prefixes: ["authority"] },
  { key: "guardrails", title: "Guardrails bắt buộc", prefixes: ["guardrails"] },
  { key: "connections", title: "Kết nối hệ thống", prefixes: ["connections"] },
];

function matchesPrefix(field, prefix) {
  return field === prefix || field.startsWith(`${prefix}.`) || field.startsWith(`${prefix}[`);
}

/** Every clarification, bucketed by the group it is about. Anything naming a
 *  field none of the seven groups own (e.g. `metadata.*`) lands in `null` —
 *  rendered once, up top, rather than silently dropped. */
function clarificationsByGroup(clarifications) {
  const byGroup = new Map(GROUPS.map((group) => [group.key, []]));
  byGroup.set(null, []);
  for (const clarification of clarifications) {
    const group = GROUPS.find((candidate) => candidate.prefixes.some((prefix) => matchesPrefix(clarification.field, prefix)));
    byGroup.get(group ? group.key : null).push(clarification);
  }
  return byGroup;
}

function ClarificationNote({ clarification }) {
  return (
    <div className="card card-warn" style={{ gap: 4, marginTop: 8 }}>
      <p style={{ display: "flex", gap: 6, alignItems: "flex-start", fontFamily: "var(--font-heading)", fontSize: "var(--text-md)", margin: 0 }}>
        <HelpCircle size={14} style={{ flexShrink: 0, marginTop: 2 }} />
        {clarification.question}
      </p>
      <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "0 0 0 20px", lineHeight: 1.5 }}>{clarification.why}</p>
    </div>
  );
}

function FieldGroup({ title, notes, children }) {
  return (
    <div className="card">
      <p className="card-title">{title}</p>
      <div style={{ fontSize: "var(--text-sm)", lineHeight: 1.6 }}>{children}</div>
      {notes.map((clarification, index) => (
        <ClarificationNote key={index} clarification={clarification} />
      ))}
    </div>
  );
}

function AudienceWho({ audience }) {
  return (
    <p style={{ margin: 0 }}>
      Vai trò: <b>{(audience.roles || []).join(", ") || "(chưa chọn)"}</b>
      {audience.departments && audience.departments.length > 0 && <> · Phòng ban: <b>{audience.departments.join(", ")}</b></>}
    </p>
  );
}

function Capabilities({ capabilities }) {
  return (
    <ul style={{ margin: 0, paddingLeft: "var(--space-4)" }}>
      {(capabilities || []).map((capability) => (
        <li key={capability.tool}>
          <span className="ab-mono">{capability.tool}</span> — {capability.scope}
          {capability.requires_approval && <> (cần duyệt)</>}
        </li>
      ))}
    </ul>
  );
}

function Prohibitions({ prohibitions }) {
  return (
    <ul style={{ margin: 0, paddingLeft: "var(--space-4)" }}>
      <li>Egress ra ngoài: <b>{prohibitions.external_egress ? "cho phép" : "chặn"}</b></li>
      {prohibitions.never_expose && prohibitions.never_expose.length > 0 && (
        <li>Không lộ: {prohibitions.never_expose.join(", ")}</li>
      )}
      {prohibitions.deny_tools && prohibitions.deny_tools.length > 0 && <li>Chặn tool: {prohibitions.deny_tools.join(", ")}</li>}
      {prohibitions.deny_topics && prohibitions.deny_topics.length > 0 && <li>Chặn chủ đề: {prohibitions.deny_topics.join(", ")}</li>}
    </ul>
  );
}

function Authority({ authority }) {
  return (
    <div>
      <p style={{ margin: 0 }}>
        Autonomy: <b>{authority.autonomy}</b> · Approval quorum: <b>{authority.approval_quorum}</b>
      </p>
      {authority.approval_matrix && Object.keys(authority.approval_matrix).length > 0 && (
        <ul style={{ margin: "6px 0 0", paddingLeft: "var(--space-4)" }}>
          {Object.entries(authority.approval_matrix).map(([tool, rule]) => (
            <li key={tool}>
              <span className="ab-mono">{tool}</span> — duyệt bởi {rule.approver_roles.join(", ")}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Guardrails({ guardrails }) {
  return (
    <p style={{ margin: 0 }}>
      Input: {guardrails.input.join(", ")}
      <br />
      Output: {guardrails.output.join(", ")}
      <br />
      Fallback: <b>{guardrails.fallback}</b>
    </p>
  );
}

function Connections({ connections }) {
  if (!connections || connections.length === 0) return <p className="text-muted" style={{ margin: 0 }}>Chưa có kết nối nào.</p>;
  return (
    <ul style={{ margin: 0, paddingLeft: "var(--space-4)" }}>
      {connections.map((connection) => (
        <li key={connection.id}>
          <span className="ab-mono">{connection.id}</span> ({connection.type}) — {connection.credential_ref}
        </li>
      ))}
    </ul>
  );
}

/** The confirm step (UC1-04 / E2). Until a draft is written down it lives only
 *  in this tab — a refresh loses it and `PATCH .../versions/{v}` has nothing to
 *  edit — so this is what turns a preview into an agent.
 *
 *  `agentId` is editable rather than fixed to the drafted `metadata.id`,
 *  because that id is also the agent's Postgres role name and container name:
 *  it can never be changed afterwards, which makes the moment before it is
 *  written the only moment anyone can fix one the meta-agent chose badly. */
function ConfirmDraft({ spec, onConfirmed, openWorkbench, notify }) {
  const [agentId, setAgentId] = useState(spec.metadata.id);
  const [busy, setBusy] = useState(false);

  useAgentActivity("processing", busy, "đang lưu bản nháp");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(null);

  async function confirm() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const row = await api.uc1Agents.confirmDraft(agentId.trim(), spec);
      setSaved(row);
      notify(`Đã lưu ${row.agent_id} version ${row.version}`);
      onConfirmed(row);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (saved) {
    return (
      <div className="card card-ok">
        <p className="card-title" style={{ display: "flex", gap: 7, alignItems: "center", margin: 0 }}>
          <Check size={16} />Đã xác nhận — <span className="ab-mono">{saved.agent_id}</span> version {saved.version}
        </p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Cấu hình đã được lưu ở trạng thái <b>draft</b>. Từ giờ mỗi lần sửa sẽ sinh một version mới và giữ nguyên bản cũ —
          version 1 không bao giờ bị ghi đè.
        </p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Còn <b>draft</b> thì nó chưa xuất hiện trong hàng đợi duyệt: phải chat thử ít nhất một lượt và chấm golden set
          trước, rồi mới xin duyệt được.
        </p>
        <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={() => openWorkbench(saved.agent_id)}>
          <FlaskConical size={14} />Mở workbench: chat thử, golden set, xin duyệt
        </button>
      </div>
    );
  }

  return (
    <div className="card">
      <p className="card-title">Xác nhận cấu hình</p>
      <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
        Bản nháp này chưa được lưu ở đâu cả. Xác nhận để ghi thành <b>version 1</b>.
      </p>
      <div className="field">
        <label>Agent ID</label>
        <input
          className="input"
          value={agentId}
          disabled={busy}
          spellCheck={false}
          onChange={(event) => setAgentId(event.target.value)}
        />
      </div>
      <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: 1.5 }}>
        Dạng <span className="ab-mono">agt_...</span> (chữ thường, số, gạch dưới). Đây cũng là tên Postgres role và tên
        container của agent, nên <b>không đổi được sau khi lưu</b>.
      </p>

      <ErrorNote>{error}</ErrorNote>

      <button
        type="button"
        className="btn btn-primary"
        style={{ alignSelf: "flex-start" }}
        onClick={confirm}
        disabled={busy || !agentId.trim()}
      >
        {busy ? <>Đang lưu…</> : <><Check size={14} />Xác nhận & lưu thành version 1</>}
      </button>
    </div>
  );
}

export default function MetaAgentDraftPage({ notify, openWorkbench }) {
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);

  useAgentActivity("builder", busy, "đang soạn cấu hình agent");
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  // A confirmed draft is frozen: re-drafting over the top of one already
  // written down would leave the screen showing a spec that no longer matches
  // the version it just created.
  const [confirmed, setConfirmed] = useState(null);

  async function submit() {
    const text = description.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    try {
      setResult(await api.metaagent.draftSpec(text));
      setConfirmed(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const grouped = result ? clarificationsByGroup(result.clarifications) : null;
  const unassigned = grouped ? grouped.get(null) : [];

  return (
    <PageShell width="narrow">
        <PageHead title="Meta-agent" hint="Describe the assistant in your own words — the meta-agent drafts an `AgentSpec` and asks about everything it had to guess." />

        <div className="ab-stack">
          <div className="field">
            <label>Mô tả agent</label>
            <textarea
              className="input"
              style={{ minHeight: 90 }}
              placeholder="Tôi cần trợ lý theo dõi công nợ quá hạn và nhắc nhân viên..."
              value={description}
              disabled={busy || !!confirmed}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>

          <ErrorNote>{error}</ErrorNote>

          <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={submit} disabled={busy || !description.trim() || !!confirmed}>
            {busy ? "Đang soạn…" : "Soạn cấu hình agent"}
          </button>
          {confirmed && (
            <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
              Đã xác nhận <span className="ab-mono">{confirmed.agent_id}</span> — soạn bản nháp mới sẽ ghi đè màn hình này
              mà không xoá version vừa lưu, nên hãy quay về Agents rồi bắt đầu lại nếu muốn một agent khác.
            </p>
          )}
        </div>

        {result && (
          <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 24 }}>
            <h2 style={{ fontSize: "var(--text-2xl)", margin: 0 }}>{result.spec.metadata.name}</h2>

            {unassigned.length > 0 && (
              <div>
                {unassigned.map((clarification, index) => (
                  <ClarificationNote key={index} clarification={clarification} />
                ))}
              </div>
            )}

            <FieldGroup title="Đối tượng sử dụng" notes={grouped.get("audience_who")}>
              <AudienceWho audience={result.spec.audience} />
            </FieldGroup>
            <FieldGroup title="Phân quyền sử dụng" notes={grouped.get("audience_scope")}>
              <p style={{ margin: 0 }}>
                Data scope: <b>{result.spec.audience.data_scope}</b>
              </p>
            </FieldGroup>
            <FieldGroup title="Agent được làm gì" notes={grouped.get("capabilities")}>
              <Capabilities capabilities={result.spec.capabilities} />
            </FieldGroup>
            <FieldGroup title="Agent không được làm gì" notes={grouped.get("prohibitions")}>
              <Prohibitions prohibitions={result.spec.prohibitions} />
            </FieldGroup>
            <FieldGroup title="Quyền hạn của agent" notes={grouped.get("authority")}>
              <Authority authority={result.spec.authority} />
            </FieldGroup>
            <FieldGroup title="Guardrails bắt buộc" notes={grouped.get("guardrails")}>
              <Guardrails guardrails={result.spec.guardrails} />
            </FieldGroup>
            <FieldGroup title="Kết nối hệ thống" notes={grouped.get("connections")}>
              <Connections connections={result.spec.connections} />
            </FieldGroup>

            <ConfirmDraft
              key={result.spec.metadata.id}
              spec={result.spec}
              notify={notify}
              onConfirmed={setConfirmed}
              openWorkbench={openWorkbench}
            />
          </div>
        )}
      </PageShell>
  );
}
