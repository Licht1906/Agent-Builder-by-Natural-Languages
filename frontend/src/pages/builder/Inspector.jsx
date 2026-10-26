import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Trash2, Flag, AlertCircle, Plus, Server, ChevronRight, X } from "lucide-react";
import * as api from "../../lib/api";
import { lookOf } from "../../lib/nodes";
import KnowledgeBasePicker from "../../components/KnowledgeBasePicker";
import AddMcpServer, { BUILT_IN_LOOK } from "../../components/AddMcpServer";
import { useLang } from "../../i18n";

/* ============================================================
   The property panel, built from the server's own `config_schema`.

   The form is generated rather than written out per node type, which is the
   whole reason `GET /workflow/node-types` serves a JSON Schema at all: a
   hand-written panel is a second description of `NODE_REGISTRY`, and it drifts
   into an input for a field the backend does not have.

   Two rules of thumb keep the panel readable (both are the user's, verbatim):

   - **Explanation lives in tooltips.** Every field's `description` goes into
     `title` — the browser shows it after a moment's hover — instead of a
     paragraph under the control. The panel states, it does not lecture.
   - **Deep config opens centred.** A prompt, a list, an action picker is a
     one-line `FieldRow` on the panel — its current state at a glance — and the
     full editor opens as a dialog in the middle of the screen. Inline walls of
     controls are what made the old panel a scroll of dread.
   ============================================================ */
export default function Inspector({ node, schema, problems, tools, readOnly, isStart, metadata, mcpNodeIds = [], onRename, onConfig, onDelete, onMakeStart, onMetadata, onToolsChanged, notify }) {
  const { t } = useLang();
  if (!node) {
    return <AgentSettings metadata={metadata} readOnly={readOnly} onMetadata={onMetadata} />;
  }

  const look = lookOf(node.type);
  const properties = (schema && schema.properties) || {};
  const nodeProblems = problems.filter((problem) => problem.node_id === node.id);

  return (
    <>
      <InspectorHead label={look.label} id={node.id} icon={look.icon} color={look.color} />
      <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: 11 }}>
      {nodeProblems.length > 0 && (
        <div className="ab-error">
          {nodeProblems.map((problem, index) => (
            <span key={index} style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
              <AlertCircle size={12} style={{ flexShrink: 0, marginTop: 3 }} />{problem.message}
            </span>
          ))}
        </div>
      )}

      <div title={t("What every connection points at. Renaming moves the wires with it.")}>
        <label className="ab-label">{t('Step id')}</label>
        <input
          className="ab-input ab-mono"
          value={node.id}
          disabled={readOnly}
          onChange={(event) => onRename(node.id, event.target.value)}
        />
      </div>

      {node.type === "mcp" ? (
        <McpNodePanel
          node={node}
          tools={tools}
          readOnly={readOnly}
          onConfig={onConfig}
          onToolsChanged={onToolsChanged}
          notify={notify}
        />
      ) : (
        Object.entries(properties).map(([name, field]) => (
          <ConfigField
            key={name}
            name={name}
            field={field}
            schema={schema}
            value={node.config[name]}
            node={node}
            mcpNodeIds={mcpNodeIds}
            readOnly={readOnly}
            onChange={(value) => onConfig(node.id, { [name]: value })}
            onPatch={(patch) => onConfig(node.id, patch)}
          />
        ))
      )}

      {Object.keys(properties).length === 0 && (
        <p style={{ fontSize: "var(--text-sm)", color: "var(--ab-muted)", margin: 0, lineHeight: 1.6 }}>
          {t("Nothing to configure — reaching this step is the behaviour.")}
        </p>
      )}

      {!readOnly && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 4 }}>
          {!isStart && (
            <button className="ab-btn ab-btn-outline ab-btn-sm" onClick={() => onMakeStart(node.id)}>
              <Flag size={13} />{t("Make this the first step")}
            </button>
          )}
          <button className="ab-btn ab-btn-danger ab-btn-sm" onClick={() => onDelete(node.id)}>
            <Trash2 size={13} />{t("Delete step")}
          </button>
        </div>
      )}
      </div>
    </>
  );
}

/** The panel's own title bar — what is selected, and the id every connection
 *  points at. Sticky, because the fields below it scroll and "which step am I
 *  editing" is the one thing that must not scroll away. */
function InspectorHead({ label, id, icon: Icon, color }) {
  const { t } = useLang();
  return (
    <div style={{ padding: "var(--space-3)", borderBottom: "1px solid var(--color-divider)", position: "sticky", top: 0, background: "var(--color-bg)", zIndex: 2 }}>
      <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "var(--color-accent)" }}>{t('Inspector')}</p>
      <p style={{ margin: "2px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", display: "flex", alignItems: "center", gap: 8 }}>
        {Icon && <Icon size={16} style={{ color: color || "var(--color-accent)", flexShrink: 0 }} />}
        {t(label)}
      </p>
      <p style={{ margin: "1px 0 0", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>{id}</p>
    </div>
  );
}

/** Shown behind the header's gear — the workflow's own settings. `disclaimer`
 *  (RAG-14 / #100) lives on `metadata` because it is per-agent config, never a
 *  platform string. */
function AgentSettings({ metadata, readOnly, onMetadata }) {
  const { t } = useLang();
  if (!metadata) return <div className="ab-empty">{t("Select a step on the canvas to configure it.")}</div>;

  return (
    <>
      <div style={{ padding: "var(--space-3)", borderBottom: "1px solid var(--color-divider)", position: "sticky", top: 0, background: "var(--color-bg)", zIndex: 2 }} title={t("These settings apply to the whole agent, not to one step.")}>
        <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "var(--color-accent)" }}>{t("Inspector")}</p>
        <p style={{ margin: "2px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{t("Agent settings")}</p>
      </div>
      <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: 11 }}>
        <div title={t("Optional. Left blank, no disclaimer is shown — this platform never adds one on its own.")}>
          <label className="ab-label">{t("Disclaimer shown under every answer")}</label>
          <textarea
            className="ab-textarea"
            style={{ minHeight: 60 }}
            disabled={readOnly}
            placeholder="e.g. Thông tin mang tính tham khảo, không thay thế tư vấn pháp lý chuyên nghiệp"
            value={metadata.disclaimer || ""}
            onChange={(event) => onMetadata({ disclaimer: event.target.value })}
          />
        </div>
      </div>
    </>
  );
}

/* ── The two building blocks of the minimal panel ── */

/** One field's state in one line, and the door to its full editor. */
function FieldRow({ label, summary, warn, title, onOpen }) {
  return (
    <button type="button" className="ab-field-row" title={title} onClick={onOpen}>
      <span className="frl">{label}</span>
      <span className={`frs${warn ? " warn" : ""}`}>{summary}</span>
      <ChevronRight size={13} style={{ flexShrink: 0, opacity: 0.55 }} />
    </button>
  );
}

/** The centred editor a FieldRow opens. Read-only mode still opens it — seeing
 *  the whole value is not an edit — with the controls inside disabled. */
function ConfigDialog({ title, open, onClose, children }) {
  const { t } = useLang();
  if (!open) return null;
  return createPortal(
    <div className="ab-dialog-backdrop" onClick={onClose}>
      <div
        className="ab-dialog"
        style={{ width: "min(520px, 94vw)", maxHeight: "min(78vh, 720px)", overflowY: "auto" }}
        onClick={(event) => event.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontWeight: 600, fontSize: "var(--text-xl)" }}>{title}</p>
          <button className="btn btn-ghost btn-sm btn-icon" style={{ width: 26, height: 26 }} title={t("Close")} onClick={onClose}>
            <X size={14} />
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body
  );
}

function labelOf(name) {
  return name.replace(/_/g, " ").replace(/^./, (character) => character.toUpperCase());
}

/* A property as the form has to read it, with any `$ref` followed.
 *
 * Pydantic writes an enum-typed field as `{"$ref": "#/$defs/McpApproval"}` and
 * puts the values under `$defs`, so a form reading only the property sees a
 * field with no `type` and no `enum` and falls through to a free-text box —
 * which for `require_approval` would be a text input the backend rejects.
 * Following the reference here rather than special-casing one field name is
 * what makes the next enum added to `NODE_REGISTRY` render without touching
 * this file. */
function resolved(field, schema) {
  const ref = field && field.$ref;
  if (!ref || !ref.startsWith("#/$defs/")) return field;
  const target = ((schema && schema.$defs) || {})[ref.slice("#/$defs/".length)];
  return target ? { ...target, ...field, description: field.description || target.description } : field;
}

/* What each enum value is called on screen.
 *
 * Kept beside the form rather than derived from the value, because a creator
 * choosing between "auto" and "always" is choosing between two sentences about
 * their own business, and the raw enum member is not one of them. Anything not
 * listed falls back to the value itself, so a new member shows up as something
 * readable rather than as a blank option. */
const ENUM_LABELS = {
  auto: "Platform decides from the action's risk",
  always: "Always require a person to approve",
  single: "One action — this step always calls the same one",
  full: "Full access — the agent picks from the allowed actions",
};

/** A long text (or list) behind a FieldRow: the row shows what is there, the
 *  dialog is where it is written. `array` joins/splits on newlines, one per
 *  line — the same transform the old inline textarea applied. */
function LongTextField({ label, description, value, readOnly, onChange, array = false }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  const empty = array ? (value || []).length === 0 : !value;
  const summary = array
    ? (empty ? t("Not filled in yet") : t("{n} item(s)", { n: value.length }))
    : (empty ? t("Not filled in yet") : String(value).slice(0, 34) + (String(value).length > 34 ? "…" : ""));
  return (
    <>
      <FieldRow label={label} summary={summary} warn={empty} title={description} onOpen={() => setOpen(true)} />
      <ConfigDialog title={label} open={open} onClose={() => setOpen(false)}>
        <textarea
          className="ab-textarea"
          style={{ minHeight: 180 }}
          autoFocus
          disabled={readOnly}
          title={array ? `${t("One per line.")} ${description || ""}` : description}
          value={array ? (value || []).join("\n") : (value || "")}
          onChange={(event) =>
            onChange(array
              ? event.target.value.split("\n").map((line) => line.trim()).filter(Boolean)
              : event.target.value)
          }
        />
      </ConfigDialog>
    </>
  );
}

function ConfigField({ name, field: declared, schema, value, node, mcpNodeIds = [], readOnly, onChange, onPatch }) {
  const { t } = useLang();
  const field = resolved(declared, schema);
  const description = field.description || "";

  // A collector points at the `mcp` step it gathers details for (ADR-0052),
  // so the choice is among the graph's own action steps — a typed id that
  // matches nothing is a collector with nothing to read, and the validator
  // would only say so later.
  if (node.type === "collect" && name === "action_node") {
    return (
      <div title={description}>
        <label className="ab-label">{t("Action step")}</label>
        <select className="ab-select" disabled={readOnly} value={value || ""} onChange={(event) => onChange(event.target.value)}>
          <option value="">{t("— choose the MCP step this collects for —")}</option>
          {mcpNodeIds.map((id) => (
            <option key={id} value={id}>{id}</option>
          ))}
        </select>
      </div>
    );
  }

  // `source_ids` names Knowledge Bases (RAG-05 / #91), so it is picked from the
  // creator's own shelves rather than typed — a whole list with its own "create
  // a shelf" form, which is exactly the depth that opens centred.
  if (node.type === "knowledge_search" && name === "source_ids") {
    return <KnowledgeSourcesField value={value} readOnly={readOnly} onChange={onChange} />;
  }

  // A `data_query` node's two RESOURCE_REF fields (DS-04): the Hệ CSDL from the
  // creator's own list, and views only from those ticked on it — never typed.
  if (node.type === "data_query" && name === "source_id") {
    // A new Hệ CSDL empties `views` in the same patch: views picked on the old
    // one mean nothing on this one, and a same-named view would be kept silently.
    return (
      <DatabaseSourceField
        value={value}
        readOnly={readOnly}
        onChange={(sourceId) => onPatch({ source_id: sourceId, views: [] })}
      />
    );
  }
  if (node.type === "data_query" && name === "views") {
    return <DatabaseViewsField sourceId={node.config.source_id} value={value} readOnly={readOnly} onChange={onChange} />;
  }

  // A closed set of values, so a closed set of choices. Placed above the
  // `array` branch because an enum *of* arrays would otherwise be drawn as a
  // free-text list, which is a box a creator can type a rejected value into.
  if (Array.isArray(field.enum)) {
    return (
      <div title={description}>
        <label className="ab-label">{t(labelOf(name))}</label>
        <select
          className="ab-select"
          disabled={readOnly}
          value={value ?? field.default ?? ""}
          onChange={(event) => onChange(event.target.value)}
        >
          {field.enum.map((option) => (
            <option key={option} value={option}>{t(ENUM_LABELS[option] || option)}</option>
          ))}
        </select>
      </div>
    );
  }

  if (field.type === "array") {
    return <LongTextField array label={t(labelOf(name))} description={description} value={value} readOnly={readOnly} onChange={onChange} />;
  }

  if (field.type === "integer" || field.type === "number") {
    return (
      <div title={description}>
        <label className="ab-label">{t(labelOf(name))}</label>
        <input
          className="ab-input"
          type="number"
          min={field.minimum ?? field.exclusiveMinimum ?? undefined}
          disabled={readOnly}
          value={value ?? ""}
          onChange={(event) => onChange(Number(event.target.value))}
        />
      </div>
    );
  }

  if (field.multiline) {
    return <LongTextField label={t(labelOf(name))} description={description} value={value} readOnly={readOnly} onChange={onChange} />;
  }

  return (
    <div title={description}>
      <label className="ab-label">{t(labelOf(name))}</label>
      <input className="ab-input" disabled={readOnly} value={value || ""} onChange={(event) => onChange(event.target.value)} />
    </div>
  );
}

/** The knowledge shelves row and its centred picker. */
function KnowledgeSourcesField({ value, readOnly, onChange }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  const count = (value || []).length;
  return (
    <>
      <FieldRow
        label={t("Knowledge Bases")}
        summary={count === 0 ? t("Not filled in yet") : t("{n} selected", { n: count })}
        warn={count === 0}
        title={t("Which shelves this step searches — click to pick")}
        onOpen={() => setOpen(true)}
      />
      <ConfigDialog title={t("Knowledge Bases")} open={open} onClose={() => setOpen(false)}>
        <KnowledgeBasePicker selected={value} onChange={onChange} disabled={readOnly} />
      </ConfigDialog>
    </>
  );
}

/** The creator's Hệ CSDL, fetched once per field that needs them. `null` while loading. */
function useDatabaseSources() {
  const [sources, setSources] = useState(null);
  useEffect(() => {
    let cancelled = false;
    api.databaseSources.list().then((rows) => !cancelled && setSources(rows)).catch(() => !cancelled && setSources([]));
    return () => { cancelled = true; };
  }, []);
  return sources;
}

function DatabaseSourceField({ value, readOnly, onChange }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  const sources = useDatabaseSources();
  const chosen = (sources || []).find((source) => String(source.id) === String(value));
  const summary = !value ? t("Not filled in yet") : chosen ? chosen.name : t("Database #{id} (missing)", { id: value });
  return (
    <>
      <FieldRow label={t("Database")} summary={summary} warn={!chosen} title={t("Which database this step reads — click to pick")} onOpen={() => setOpen(true)} />
      <ConfigDialog title={t("Database")} open={open} onClose={() => setOpen(false)}>
        {sources && sources.length === 0 && (
          <p className="text-muted" style={{ margin: 0 }}>{t("No database registered yet — add one on the Data sources page.")}</p>
        )}
        <div role="radiogroup" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {(sources || []).map((source) => (
            <label key={source.id} style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                type="radio"
                name="data-query-source"
                aria-label={source.name}
                disabled={readOnly}
                checked={String(source.id) === String(value)}
                onChange={() => onChange(String(source.id))}
              />
              {source.name}
            </label>
          ))}
        </div>
      </ConfigDialog>
    </>
  );
}

function DatabaseViewsField({ sourceId, value, readOnly, onChange }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  const sources = useDatabaseSources();
  const chosen = new Set(value || []);
  const source = (sources || []).find((entry) => String(entry.id) === String(sourceId));
  const allowed = source ? source.allowed_views : [];
  const stale = [...chosen].filter((view) => source && !allowed.includes(view));
  const toggle = (view) => onChange(allowed.filter((name) => (name === view ? !chosen.has(name) : chosen.has(name))));
  return (
    <>
      <FieldRow
        label={t("Views")}
        summary={chosen.size === 0 ? t("Not filled in yet") : t("{n} selected", { n: chosen.size })}
        warn={chosen.size === 0 || stale.length > 0}
        title={t("Which views this step may read — click to pick")}
        onOpen={() => setOpen(true)}
      />
      <ConfigDialog title={t("Views")} open={open} onClose={() => setOpen(false)}>
        {!sourceId && <p className="text-muted" style={{ margin: 0 }}>{t("Pick a database first, then its ticked views appear here.")}</p>}
        {sourceId && source && allowed.length === 0 && (
          <p className="text-muted" style={{ margin: 0 }}>{t("This database has no ticked views yet — tick some on the Data sources page.")}</p>
        )}
        {allowed.map((view) => (
          <label key={view} className="ab-mono" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" aria-label={view} disabled={readOnly} checked={chosen.has(view)} onChange={() => toggle(view)} />
            {view}
          </label>
        ))}
        {stale.map((view) => (
          <p key={view} className="ab-mono" style={{ margin: 0, color: "#f8a5a5" }}>{t("{view} (not ticked)", { view })}</p>
        ))}
      </ConfigDialog>
    </>
  );
}

/** What the platform decided about the chosen action. Every one of these is
 *  derived server-side from the connection's own introspection, so it is a fact
 *  about the tool rather than a claim by the workflow asking for it. */
function ActionNote({ action }) {
  const { t } = useLang();
  if (!action) return null;
  const risky = action.risk_level === "high";
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
      <span className="ab-chip" style={risky ? { color: "#f87171", borderColor: "rgba(239,68,68,.3)" } : undefined}>
        {t("risk")}: {t(action.risk_level)}
      </span>
      <span className="ab-chip">{action.read_only ? t("read-only") : t("has side effects")}</span>
      {action.requires_human_approval && (
        <span className="ab-chip" style={{ color: "var(--ab-pink)", borderColor: "rgba(236,72,153,.3)" }}>{t("needs approval")}</span>
      )}
    </div>
  );
}

/* ============================================================
   The MCP node's own panel — the one node type whose properties are not a form.

   Two things on the panel, in the order they matter: the connector this step
   is wired to, drawn at headline size (the chip is also the way to change it),
   and one row summarising what the step may call. Everything that *sets* those
   choices — the mode, the action or the allowed list, the approval gate —
   opens centred, because a checkbox per action inline is the wall the user
   asked this panel to stop being.
   ============================================================ */
function McpNodePanel({ node, tools, readOnly, onConfig, onToolsChanged, notify }) {
  const { t } = useLang();
  const [picking, setPicking] = useState(false);
  const [configuring, setConfiguring] = useState(false);
  const attached = tools.find((tool) => tool.id === node.config.tool_id) || null;
  const fullMode = node.config.action_mode === "full";
  const chosen = node.config.allowed_actions || [];
  const all = chosen.length === 0;

  const set = (patch) => onConfig(node.id, patch);

  const summary = !attached
    ? ""
    : fullMode
      ? t("Full access — {n} actions", { n: all ? attached.actions.length : chosen.length })
      : (node.config.action || t("No action chosen yet"));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {/* What this step is wired to — and the *same* control changes it.
          Adding is only offered when nothing is attached; once a connector is
          there, the chip is the door back to the picker. */}
      <div className="mcp-attached">
        {attached ? (
          <button
            type="button"
            className="mcp-chip mcp-chip-lg"
            onClick={() => !readOnly && setPicking(true)}
            disabled={readOnly}
            title={t("Click to change the connected service")}
          >
            <ConnectorMark tool={attached} big />
          </button>
        ) : (
          !readOnly && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setPicking(true)}>
              <Plus size={12} />{t("Add MCP server")}
            </button>
          )
        )}
      </div>

      {!attached && (
        <p style={{ fontSize: "var(--text-xs)", color: "var(--ab-warn)", margin: 0, lineHeight: 1.5 }}>
          {t("Nothing wired yet — add a server, or pick one already connected.")}
        </p>
      )}

      <AddMcpServer
        open={picking}
        onClose={() => setPicking(false)}
        onRegistered={onToolsChanged}
        notify={notify}
        onPick={(tool) => set({ tool_id: tool.id, action: "", allowed_actions: [] })}
      />

      {attached && (
        <FieldRow
          label={t("Actions & approval")}
          summary={[
            summary,
            node.config.require_approval === "always" ? t("always approved by a person") : null,
          ].filter(Boolean).join(" · ")}
          warn={!fullMode && !node.config.action}
          title={t("What this step may call, and whether a person approves it — click to configure")}
          onOpen={() => setConfiguring(true)}
        />
      )}

      <ConfigDialog
        title={attached ? `${t("Actions & approval")} — ${(BUILT_IN_LOOK[attached.id] || {}).label || attached.name}` : t("Actions & approval")}
        open={configuring && Boolean(attached)}
        onClose={() => setConfiguring(false)}
      >
        {attached && (
          <>
            <div title={t("One action makes this step deterministic; full access lets the agent choose per run.")}>
              <label className="ab-label">{t("What this step may call")}</label>
              <select
                className="ab-select"
                disabled={readOnly}
                value={node.config.action_mode || "single"}
                onChange={(event) => set({ action_mode: event.target.value })}
              >
                <option value="single">{t("One action — always the same one")}</option>
                <option value="full">{t("Full access — the agent picks")}</option>
              </select>
            </div>

            {!fullMode && (
              <div>
                <label className="ab-label">{t("Action")}</label>
                <select
                  className="ab-select"
                  disabled={readOnly}
                  value={node.config.action || ""}
                  onChange={(event) => set({ action: event.target.value })}
                >
                  <option value="">{t("— not chosen —")}</option>
                  {attached.actions.map((action) => (
                    <option key={action.name} value={action.name}>{action.name}</option>
                  ))}
                </select>
                <ActionNote action={attached.actions.find((entry) => entry.name === node.config.action)} />
              </div>
            )}

            {fullMode && (
              <div title={t("The riskiest action ticked decides whether this step needs human approval.")}>
                <label className="ab-label">{t("Actions the agent may pick from")}</label>
                <div className="mcp-abilities" style={{ marginTop: 4 }}>
                  <label className="mcp-ability" style={{ cursor: readOnly ? "default" : "pointer" }}>
                    <input type="checkbox" checked={all} disabled={readOnly} onChange={() => set({ allowed_actions: [] })} />
                    <span style={{ fontWeight: 500 }}>{t("All of them")}</span>
                  </label>
                  {attached.actions.map((action) => (
                    <label key={action.name} className="mcp-ability" style={{ cursor: readOnly || all ? "default" : "pointer" }}>
                      <input
                        type="checkbox"
                        checked={all || chosen.includes(action.name)}
                        disabled={readOnly || all}
                        onChange={() => {
                          const next = chosen.includes(action.name)
                            ? chosen.filter((entry) => entry !== action.name)
                            : [...chosen, action.name];
                          // Every box ticked means what no box ticked means; store
                          // the simpler of the two so the config does not go stale
                          // when the connection gains an action later.
                          set({ allowed_actions: next.length === attached.actions.length ? [] : next });
                        }}
                      />
                      <span className="ab-mono">{action.name}</span>
                      {action.risk_level === "high" && (
                        <span className="tag tag-danger" style={{ marginLeft: "auto" }}>high</span>
                      )}
                    </label>
                  ))}
                </div>
              </div>
            )}

            <div title={t("The riskiest action ticked decides whether this step needs human approval.")}>
              <label className="ab-label">{t("Approval")}</label>
              <select
                className="ab-select"
                disabled={readOnly}
                value={node.config.require_approval || "auto"}
                onChange={(event) => set({ require_approval: event.target.value })}
              >
                <option value="auto">{t("Platform decides from the action's risk")}</option>
                <option value="always">{t("Always require a person to approve")}</option>
              </select>
            </div>
          </>
        )}
      </ConfigDialog>
    </div>
  );
}

/** One connector's mark — its own logo where the platform ships one, a generic
 *  server glyph where a creator brought their own. */
function ConnectorMark({ tool, big = false }) {
  const look = BUILT_IN_LOOK[tool.id] || {};
  const Icon = look.icon || Server;
  return (
    <>
      <span style={{ color: look.color || "var(--color-accent)", display: "flex" }}>
        <Icon size={big ? 22 : 18} />
      </span>
      <span style={{ fontSize: big ? 15 : 13.5, fontWeight: big ? 500 : 400 }}>{look.label || tool.name}</span>
    </>
  );
}
