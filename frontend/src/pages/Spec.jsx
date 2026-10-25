import { useCallback, useEffect, useState } from "react";
import { Layers, Save, CheckCircle2, Plus, Trash2 } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner, StatusBadge } from "../components/ui";
import { useLang } from "../i18n";
import { useAgentActivity } from "../components/agent-presence";
import { AgentMascot } from "../components/mascot";

/* The two closed enums the spec carries. Both are `StrEnum`s on the backend —
   a value not in these lists is one the API would refuse, so the form offers
   exactly these and nothing else. */
/* `picked` is a kind whose `ref` is chosen from a list rather than typed: a
   Knowledge Base for `uploaded_file`, a Hệ CSDL for `database` (ADR-0059 §2).
   Only `connected_account` still takes a typed connection id. */
const DATA_KINDS = [
  { id: "uploaded_file", label: "Uploaded file", addressable: true, picked: true },
  { id: "connected_account", label: "Connected account", addressable: true, picked: false },
  { id: "web", label: "Web", addressable: false, picked: false },
  { id: "user_input", label: "User input", addressable: false, picked: false },
  { id: "database", label: "Database", addressable: true, picked: true },
];

const CRITERION_CATEGORIES = [
  { id: "workflow_success", label: "Workflow completes" },
  { id: "tool_correctness", label: "Right tool called" },
  { id: "human_approval_compliance", label: "Approval respected" },
  { id: "source_groundedness", label: "Grounded in sources" },
  { id: "correctness", label: "Factually correct" },
  { id: "relevance", label: "Relevant answer" },
];

const isAddressable = (kind) => DATA_KINDS.some((entry) => entry.id === kind && entry.addressable);
const isPicked = (kind) => DATA_KINDS.some((entry) => entry.id === kind && entry.picked);

/** Trim the way the backend does before it decides a list is empty, so the
 *  "what still needs filling in" hint on screen matches what a save would say. */
const cleaned = (lines) => (lines || []).map((line) => line.trim()).filter(Boolean);

/* The four plain `list[str]` fields, edited one sentence per line. Declared
   rather than written out four times, because the only thing that differs
   between them is the label and the prompt. */
const LIST_FIELDS = [
  { key: "users", label: "Users", rows: 3, hint: "Who will use it" },
  { key: "actions", label: "Actions", rows: 4, hint: "What it must be able to do" },
  { key: "expected_outcome", label: "Expected outcome", rows: 3, hint: "What the user gets when it finishes" },
  { key: "prohibitions", label: "Prohibitions", rows: 3, hint: "What it must never do under any circumstances" },
];

export default function SpecPage({ agentId, versionId, agentName, openBuilder, notify }) {
  const { t } = useLang();
  const [spec, setSpec] = useState(null);
  const [status, setStatus] = useState(null);
  const [number, setNumber] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [hasWorkflow, setHasWorkflow] = useState(false);

  useAgentActivity("processing", busy === "save", t("saving the specification"));
  useAgentActivity("processing", busy === "confirm", t("confirming the specification"));
  useAgentActivity("builder", busy === "generate", t("composing the workflow"));
  useAgentActivity("exploring", loading, t("opening the specification"));

  const confirmed = status && status !== "spec_pending_confirmation";

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await api.spec.read(agentId, versionId);
      setSpec(response.spec);
      setStatus(response.status);
      setNumber(response.version_number);
      // A version past the spec stage may already hold a graph. Asking is the
      // only way to know, and a 404 here is an answer rather than a failure.
      try {
        await api.workflow.read(agentId, versionId);
        setHasWorkflow(true);
      } catch {
        setHasWorkflow(false);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [agentId, versionId]);

  useEffect(() => { load(); }, [load]);

  const patch = (fields) => setSpec((current) => ({ ...current, ...fields }));

  /** Everything the API refuses an empty value for — reported before the round
   *  trip so the buttons can say why they are disabled. */
  function missing() {
    if (!spec) return ["spec"];
    const gaps = [];
    if (!spec.goal || !spec.goal.trim()) gaps.push("Goal");
    if (cleaned(spec.users).length === 0) gaps.push("Users");
    if ((spec.data || []).filter((source) => source.name.trim()).length === 0) gaps.push("Data sources");
    if (cleaned(spec.actions).length === 0) gaps.push("Actions");
    if (cleaned(spec.expected_outcome).length === 0) gaps.push("Expected outcome");
    if ((spec.success_criteria || []).filter((item) => item.text.trim()).length === 0) gaps.push("Success criteria");
    if (cleaned(spec.prohibitions).length === 0) gaps.push("Prohibitions");
    return gaps;
  }

  /** The document as the API wants it: blank rows dropped, and no `ref` on a
   *  kind that has nothing to point at (the backend refuses that pairing). */
  function payload() {
    return {
      goal: (spec.goal || "").trim(),
      users: cleaned(spec.users),
      data: (spec.data || [])
        .filter((source) => source.name.trim())
        .map((source) => ({
          name: source.name.trim(),
          kind: source.kind,
          ref: isAddressable(source.kind) && source.ref && source.ref.trim() ? source.ref.trim() : null,
        })),
      actions: cleaned(spec.actions),
      expected_outcome: cleaned(spec.expected_outcome),
      success_criteria: (spec.success_criteria || [])
        .filter((item) => item.text.trim())
        .map((item) => ({ text: item.text.trim(), category: item.category })),
      prohibitions: cleaned(spec.prohibitions),
    };
  }

  async function run(kind, action) {
    setBusy(kind);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  /** Every `uploaded_file` source's Knowledge Base, as the agent's own
   *  assignment (`agent_kb_links`) — the fact RAG-13's shelf list reads to
   *  say "agent nào đang dùng". Best-effort: a failure here is not a reason
   *  to fail the save that provoked it, since the assignment can always be
   *  corrected later from the Knowledge Bases screen. */
  async function syncKnowledgeBaseAssignment(body) {
    const kbIds = body.data
      .filter((source) => source.kind === "uploaded_file" && source.ref)
      .map((source) => Number(source.ref));
    try {
      await api.agents.setKnowledgeBases(agentId, kbIds);
    } catch {
      // ignored — best-effort, see above
    }
  }

  const save = () => run("save", async () => {
    const body = payload();
    const response = await api.spec.revise(agentId, versionId, body);
    await syncKnowledgeBaseAssignment(body);
    setSpec(response.spec);
    setStatus(response.status);
    notify(t("Specification saved"));
  });

  const confirm = () => run("confirm", async () => {
    const body = payload();
    await api.spec.revise(agentId, versionId, body);
    await syncKnowledgeBaseAssignment(body);
    const response = await api.spec.confirm(agentId, versionId);
    setSpec(response.spec);
    setStatus(response.status);
    notify(t("Specification confirmed — it is immutable from here"));
  });

  /* `templateId` null is the ordinary path: the platform composes a graph for
     this spec's own problem. A template is what the gallery below passes, and
     it is always the creator's own choice — never something inferred from how
     their description happened to read. */
  const generate = (templateId = null) => run("generate", async () => {
    const draft = await api.workflow.generate(agentId, versionId, templateId);
    notify(templateId ? t("Draft workflow built from the template you picked") : t("Workflow composed for your specification"));
    openBuilder(draft.version_id);
  });

  const gaps = spec ? missing() : [];

  return (
    <PageShell>
        {/* ── Title row: what this is, and the two decisions available on it ── */}
        <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 18, marginBottom: 8, flexWrap: "wrap", animation: "agUp .35s ease both" }}>
          <div>
            <PageHead title={t('Agent Solution Specification')} />
            <p className="ab-mono" style={{ margin: "0 0 var(--space-1)", fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>
              {agentName || "Agent"} · version #{number ?? "…"}
            </p>
            <p style={{ margin: 0, fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 60%, transparent)", maxWidth: "70ch" }}>
              {confirmed
                ? t("Confirmed and immutable. Changing the business intent means a new version.")
                : t("Seven fields the workflow is generated from. Confirm freezes them — after that a change forks a new version.")}
            </p>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flex: "none", flexWrap: "wrap" }}>
            <StatusBadge status={status} />
            {!confirmed && (
              <>
                <button className="btn btn-secondary" onClick={save} disabled={!!busy || !spec} style={{ height: 38 }}>
                  {busy === "save" ? <Spinner size={14} /> : <Save size={14} />}{t("Save draft")}
                </button>
                <button className="btn btn-primary" onClick={confirm} disabled={!!busy || !spec || gaps.length > 0} style={{ height: 38 }}>
                  {busy === "confirm" ? <Spinner size={14} /> : <CheckCircle2 size={15} />}{t("Confirm specification")}
                </button>
              </>
            )}
            {confirmed && !hasWorkflow && (
              <button className="btn btn-primary" onClick={() => generate()} disabled={!!busy} style={{ height: 38 }}>
                {busy === "generate" ? <Spinner size={14} /> : <Layers size={15} />}{t("Generate workflow")}
              </button>
            )}
            {confirmed && hasWorkflow && (
              <button className="btn btn-primary" onClick={() => openBuilder(versionId)} style={{ height: 38 }}>
                <Layers size={15} />{t("Open the canvas")}
              </button>
            )}
          </div>
        </div>

        <ErrorNote>{error}</ErrorNote>

        {gaps.length > 0 && !confirmed && spec && (
          <div style={{ border: "1px solid var(--color-accent)", background: "var(--color-accent-100)", color: "var(--color-accent-800)", padding: "var(--space-3) var(--space-4)", margin: "14px 0 0", fontSize: "var(--text-sm)" }}>
            {t("Still to fill in: {list} — all seven are required before the specification can be confirmed.", { list: gaps.map(t).join(", ") })}
          </div>
        )}

        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}

        {spec && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "1.25fr .95fr", gap: 20, marginTop: 22, alignItems: "start" }}>
              {/* ── Left: the prose fields ── */}
              <div className="blueprint" style={{ padding: "var(--space-4)" }}>
                <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                <div className="field" style={{ marginBottom: 16 }}>
                  <label htmlFor="goal">{t('Goal')}</label>
                  <textarea
                    id="goal"
                    className="input"
                    rows={3}
                    disabled={confirmed}
                    value={spec.goal || ""}
                    onChange={(e) => patch({ goal: e.target.value })}
                    placeholder={t("The business problem this agent solves")}
                  />
                </div>
                {LIST_FIELDS.map((entry) => (
                  <div className="field" key={entry.key} style={{ marginBottom: 16 }}>
                    <label>{t(entry.label)}</label>
                    <textarea
                      className="input"
                      rows={entry.rows}
                      disabled={confirmed}
                      placeholder={t(entry.hint)}
                      value={(spec[entry.key] || []).join("\n")}
                      onChange={(e) => patch({ [entry.key]: e.target.value.split("\n") })}
                    />
                    <p style={{ margin: "5px 0 0", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>{t("One per line.")}</p>
                  </div>
                ))}
              </div>

              {/* ── Right: the two structured lists ── */}
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <DataSources sources={spec.data || []} disabled={confirmed} onChange={(data) => patch({ data })} />
                <Criteria criteria={spec.success_criteria || []} disabled={confirmed} onChange={(success_criteria) => patch({ success_criteria })} />
              </div>
            </div>

            {confirmed && !hasWorkflow && <TemplateGallery busy={!!busy} onPick={generate} />}
          </>
        )}
      </PageShell>
  );
}

/** The starting shapes on offer, for a creator who would rather pick one.
 *
 *  Deliberately *below* the Generate button and deliberately collapsed. The
 *  platform composing a graph for this particular problem is the better answer
 *  in general — a template is a shape somebody else's problem had — so the
 *  gallery is the shortcut, not the main road.
 *
 *  Silent about a listing that fails. It is a convenience, and an error banner
 *  about a shortcut would read as though the Generate button above it were
 *  broken too. */
function TemplateGallery({ busy, onPick }) {
  const { t } = useLang();
  const [templates, setTemplates] = useState([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.workflow.templates()
      .then((listed) => !cancelled && setTemplates(listed))
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  if (!templates.length) return null;

  return (
    <div className="blueprint" style={{ padding: "var(--space-4)", marginTop: 20 }}>
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <button
        className="btn btn-ghost"
        style={{ padding: 0 }}
        onClick={() => setOpen(!open)}
        title={t("Generating without picking one composes a workflow for your own specification. A template is a ready-made shape for a common agent — quicker to start from, and you edit it on the canvas either way.")}
      >
        <Layers size={14} />
        {open ? t("Hide the template library") : t("Or start from a template ({n})", { n: templates.length })}
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 14 }}>
          {templates.map((template) => (
            <div key={template.id} style={{ display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between", padding: "var(--space-3)", border: "1px solid var(--color-divider)" }}>
              <div style={{ minWidth: 0 }}>
                <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-lg)" }}>{template.name}</p>
                <p style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)", margin: "4px 0 0", lineHeight: 1.55 }}>
                  {template.description}
                </p>
                <p style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 50%, transparent)", margin: "6px 0 0" }}>
                  {t("{n} nodes", { n: template.node_count })}
                  {template.required_data_kinds.length > 0 && ` · ${t("needs")} ${template.required_data_kinds.join(", ")}`}
                </p>
              </div>
              <button className="btn btn-secondary" style={{ flexShrink: 0, fontSize: "var(--text-sm)" }} disabled={busy} onClick={() => onPick(template.id)}>
                {t("Use this")}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DataSources({ sources, disabled, onChange }) {
  const { t } = useLang();
  const [shelves, setShelves] = useState([]);
  // The creator's Hệ CSDL (DS-02) — a `database` source's `ref` is one of these.
  const [databases, setDatabases] = useState([]);

  const update = (index, fields) =>
    onChange(sources.map((source, position) => (position === index ? { ...source, ...fields } : source)));

  const loadShelves = () => api.knowledgeBases.list().then(setShelves).catch(() => {});

  // An `uploaded_file` source is addressed by a Knowledge Base id (RAG-13 /
  // #99), picked from the creator's own shelves rather than typed — the
  // interview never invents one, since a model reading a description cannot
  // know which shelf was meant. This is where a data source first gets a real
  // address, or — via "Tạo tủ mới" — where the shelf itself first gets built.
  useEffect(() => {
    let cancelled = false;
    api.knowledgeBases.list().then((kbs) => !cancelled && setShelves(kbs)).catch(() => {});
    api.databaseSources.list().then((rows) => !cancelled && setDatabases(rows)).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="blueprint" style={{ padding: "var(--space-4)" }}>
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <p style={{ margin: "0 0 12px", fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 60%, transparent)" }}>{t('Data sources')}</p>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {sources.length === 0 && (
          <p style={{ margin: 0, fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
            {t("Nothing listed yet — at least one is required.")}
          </p>
        )}
        {sources.map((source, index) => (
          <div key={index} style={{ display: "flex", flexDirection: "column", gap: 7, border: "1px solid var(--color-divider)", padding: "var(--space-2) var(--space-3)" }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                className="input"
                style={{ flex: 1, minWidth: 0 }}
                disabled={disabled}
                value={source.name}
                placeholder={t("What the source is called")}
                onChange={(e) => update(index, { name: e.target.value })}
              />
              {!disabled && (
                <button
                  className="btn btn-secondary btn-icon"
                  style={{ width: 26, height: 26, padding: "var(--space-1)", flex: "none" }}
                  onClick={() => onChange(sources.filter((_, position) => position !== index))}
                  title={t("Remove")}
                  aria-label={t("Remove")}
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <select
                className="input"
                style={{ flex: "1 1 130px", minWidth: 0 }}
                disabled={disabled}
                value={source.kind}
                onChange={(e) => update(index, { kind: e.target.value, ref: isAddressable(e.target.value) ? source.ref : null })}
              >
                {DATA_KINDS.map((kind) => <option key={kind.id} value={kind.id}>{t(kind.label)}</option>)}
              </select>
              {!isPicked(source.kind) && (
                <input
                  className="input"
                  style={{ flex: "1 1 130px", minWidth: 0 }}
                  disabled={disabled || !isAddressable(source.kind)}
                  value={source.ref || ""}
                  placeholder={isAddressable(source.kind) ? t("Connection id") : t("no address")}
                  onChange={(e) => update(index, { ref: e.target.value })}
                />
              )}
            </div>
            {source.kind === "uploaded_file" && (
              <KnowledgeBaseChips
                shelves={shelves}
                selected={source.ref}
                disabled={disabled}
                onSelect={(ref) => update(index, { ref })}
                onCreated={loadShelves}
              />
            )}
            {source.kind === "database" && (
              <DatabaseChips
                databases={databases}
                selected={source.ref}
                disabled={disabled}
                onSelect={(ref) => update(index, { ref })}
              />
            )}
          </div>
        ))}
      </div>

      {!disabled && (
        <button
          className="btn btn-secondary"
          style={{ marginTop: 11, fontSize: "var(--text-sm)" }}
          onClick={() => onChange([...sources, { name: "", kind: "uploaded_file", ref: null }])}
        >
          <Plus size={13} />{t("Add a source")}
        </button>
      )}
      <p style={{ fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)", margin: "9px 0 0", lineHeight: 1.5 }}>
        {t("A Knowledge Search node searches the Knowledge Bases you pick here.")}
      </p>
    </div>
  );
}

/** One chip per `{id, name}` the `ref` may name, the chosen one lit, and — when
 *  the stored `ref` is none of them — a red chip saying so rather than
 *  dropping it. The row both picked kinds share; each wraps it with what is
 *  its own (a Knowledge Base can be created on the spot, a Hệ CSDL cannot). */
function ChoiceChips({ items, selected, disabled, onSelect, missingLabel }) {
  return (
    <>
      {items.map((item) => {
        const on = String(selected) === String(item.id);
        return (
          <button
            key={item.id}
            type="button"
            disabled={disabled}
            className="ab-chip"
            style={on ? { color: "var(--ab-primary)", borderColor: "rgba(0,212,168,.4)", background: "rgba(0,212,168,.08)" } : undefined}
            onClick={() => onSelect(on ? null : String(item.id))}
          >
            {item.name}
          </button>
        );
      })}
      {selected && !items.some((item) => String(item.id) === String(selected)) && (
        <span className="ab-chip" style={{ color: "#f8a5a5", borderColor: "rgba(239,68,68,.3)" }}>
          {missingLabel}
        </span>
      )}
    </>
  );
}

/** Quick-select chips for a single `uploaded_file` source's Knowledge Base,
 *  plus "Tạo tủ mới" — RAG-13 (#99)'s own words for this exact spot: creating
 *  a shelf in the middle of building an agent, not as a detour to another
 *  screen and back. */
function KnowledgeBaseChips({ shelves, selected, disabled, onSelect, onCreated }) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  async function create(event) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      const kb = await api.knowledgeBases.create(trimmed);
      await onCreated();
      onSelect(String(kb.id));
      setCreating(false);
      setName("");
    } catch {
      // The dialog stays open with what was typed — the error itself surfaces
      // through the page's own toast, this row just does not lose the draft.
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
      <ChoiceChips
        items={shelves}
        selected={selected}
        disabled={disabled}
        onSelect={onSelect}
        missingLabel={`Knowledge Base #${selected} (missing)`}
      />
      {!disabled && !creating && (
        <button type="button" className="ab-btn ab-btn-ghost ab-btn-sm" onClick={() => setCreating(true)}>
          <Plus size={12} />Tạo tủ mới
        </button>
      )}
      {!disabled && creating && (
        <form onSubmit={create} style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            autoFocus
            className="ab-input"
            style={{ fontSize: "var(--text-xs)", width: 160 }}
            placeholder="Tên Knowledge Base mới"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setCreating(false)}
          />
          <button className="ab-btn ab-btn-primary ab-btn-sm" type="submit" disabled={busy || !name.trim()}>
            {busy ? <Spinner size={12} /> : "Tạo"}
          </button>
          <button type="button" className="ab-btn ab-btn-ghost ab-btn-sm" onClick={() => setCreating(false)}>Huỷ</button>
        </form>
      )}
    </div>
  );
}

/** Quick-select chips for a `database` source's Hệ CSDL — the same "picked,
 *  never typed" rule as `KnowledgeBaseChips`, because a typed id is the one a
 *  creator gets wrong without noticing. A source with no `ref` yet says so:
 *  the workflow cannot query a database nobody attached. */
function DatabaseChips({ databases, selected, disabled, onSelect }) {
  const { t } = useLang();
  const muted = { fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" };

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
      {!selected && (
        <span className="ab-chip" style={{ color: "#f5c26b", borderColor: "rgba(245,194,107,.35)" }}>
          {t("Not attached yet")}
        </span>
      )}
      <ChoiceChips
        items={databases}
        selected={selected}
        disabled={disabled}
        onSelect={onSelect}
        missingLabel={t("Database #{id} (missing)", { id: selected })}
      />
      {databases.length === 0 && (
        <span style={muted}>{t("No database registered yet — add one on the Data sources page.")}</span>
      )}
    </div>
  );
}

function Criteria({ criteria, disabled, onChange }) {
  const { t } = useLang();
  const update = (index, fields) =>
    onChange(criteria.map((item, position) => (position === index ? { ...item, ...fields } : item)));

  return (
    <div className="blueprint" style={{ padding: "var(--space-4)" }}>
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <p style={{ margin: "0 0 12px", fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 60%, transparent)" }}>{t('Success criteria')}</p>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {criteria.length === 0 && (
          <p style={{ margin: 0, fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
            {t("Nothing listed yet — evaluation has nothing to grade without these.")}
          </p>
        )}
        {criteria.map((item, index) => (
          <div key={index} style={{ border: "1px solid var(--color-divider)", padding: "var(--space-2) var(--space-3)", display: "flex", flexDirection: "column", gap: 7 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                className="input"
                style={{ flex: 1, minWidth: 0 }}
                disabled={disabled}
                value={item.text}
                placeholder={t("What counts as doing this right")}
                onChange={(e) => update(index, { text: e.target.value })}
              />
              {!disabled && (
                <button
                  className="btn btn-secondary btn-icon"
                  style={{ width: 26, height: 26, padding: "var(--space-1)", flex: "none" }}
                  onClick={() => onChange(criteria.filter((_, position) => position !== index))}
                  title={t("Remove")}
                  aria-label={t("Remove")}
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>
            {disabled ? (
              <span className="tag tag-accent" style={{ alignSelf: "flex-start" }}>
                {(CRITERION_CATEGORIES.find((entry) => entry.id === item.category) || {}).label || item.category}
              </span>
            ) : (
              <select className="input" value={item.category} onChange={(e) => update(index, { category: e.target.value })}>
                {CRITERION_CATEGORIES.map((category) => <option key={category.id} value={category.id}>{t(category.label)}</option>)}
              </select>
            )}
          </div>
        ))}
      </div>

      {!disabled && (
        <button
          className="btn btn-secondary"
          style={{ marginTop: 11, fontSize: "var(--text-sm)" }}
          onClick={() => onChange([...criteria, { text: "", category: "correctness" }])}
          title={t("Evaluation writes its test cases from these. The first three categories are graded by reading the run transcript; the last three by an LLM judge.")}
        >
          <Plus size={13} />{t("Add a criterion")}
        </button>
      )}
    </div>
  );
}
