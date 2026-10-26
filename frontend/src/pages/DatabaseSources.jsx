import { useCallback, useEffect, useState } from "react";
import { Check, Database, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import { useLang } from "../i18n";
import { AgentMascot } from "../components/mascot";
import AccessRulesEditor from "./AccessRulesEditor";

/* ============================================================
   The Databases tab of "Nguồn dữ liệu" (DS-02 / ADR-0059 §2): register a Hệ
   CSDL by its DSN, test it, see its views with their row counts, tick the
   ones an agent may read.

   The DSN is typed into a password box and never shown again — the API only
   ever answers `has_dsn`. A base table is listed, so a creator can see it was
   found, but it has no checkbox: ADR-0059 lets an agent read views only, and
   the API refuses one ticked anyway.
   ============================================================ */

//: What the dialect select offers, each with the DSN shape it takes. MySQL
//: (DS-07) joins when its dialect lands, not before.
const DIALECTS = [
  { id: "postgresql", label: "PostgreSQL", placeholder: "postgresql://user:password@host:5432/erp" },
  { id: "sqlserver", label: "SQL Server", placeholder: "Server=host,1433;Database=erp;User Id=user;Password=password" },
];

const EMPTY_FORM = { name: "", dialect: "postgresql", dsn: "" };

export default function DatabaseSourcesTab({ tabs, notify }) {
  const { t } = useLang();
  const [sources, setSources] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [composing, setComposing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSources(await api.databaseSources.list());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  /** One source changed on the server — swap it in without reloading the rest. */
  const replace = (updated) => setSources((current) => current.map((row) => (row.id === updated.id ? updated : row)));

  async function afterCreate(created) {
    setComposing(false);
    setSources((current) => [created, ...current]);
    try {
      replace(await api.databaseSources.test(created.id));
    } catch (err) {
      notify(err.message, "error");
    }
  }

  async function remove(source) {
    try {
      await api.databaseSources.remove(source.id);
      setSources((current) => current.filter((row) => row.id !== source.id));
      notify(t("Deleted “{name}”", { name: source.name }));
    } catch (err) {
      const detail = err.body && err.body.detail;
      notify(err.status === 409 && detail && detail.message ? detail.message : err.message, "error");
    }
  }

  return (
    <PageShell width="narrow">
        <PageHead title={t("Data sources")} hint={t("Where an agent reads from: documents on a shelf, or a company database it queries by the asker's role.")}>
          {!composing && (
            <button type="button" className="btn btn-primary" onClick={() => setComposing(true)}>
              <Plus size={14} />{t("Add a database")}
            </button>
          )}
        </PageHead>

        {tabs}

        {composing && <CreateDatabaseSource onCancel={() => setComposing(false)} onCreated={afterCreate} />}

        <ErrorNote>{error}</ErrorNote>

        {loading && <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}><AgentMascot state="exploring" size={78} /></div>}

        {!loading && sources.length === 0 && (
          <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>
            {t("No database yet — add one to let an agent answer from it, scoped to whoever is asking.")}
          </div>
        )}

        <div className="ab-stack">
          {sources.map((source) => (
            <DatabaseSourceCard key={source.id} source={source} onChanged={replace} onRemove={() => remove(source)} notify={notify} />
          ))}
        </div>
    </PageShell>
  );
}

function CreateDatabaseSource({ onCancel, onCreated }) {
  const { t } = useLang();
  const [form, setForm] = useState(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const edit = (fields) => setForm((current) => ({ ...current, ...fields }));

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onCreated(await api.databaseSources.create({ name: form.name.trim(), dialect: form.dialect, dsn: form.dsn.trim() }));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={submit} style={{ marginBottom: 18, gap: 12 }}>
      <div className="field">
        <label htmlFor="ds-name">{t("Name")}</label>
        <input id="ds-name" className="input" value={form.name} placeholder="ERP staging" onChange={(e) => edit({ name: e.target.value })} />
      </div>
      <div className="field">
        <label htmlFor="ds-dialect">{t("Database type")}</label>
        <select id="ds-dialect" className="input" value={form.dialect} onChange={(e) => edit({ dialect: e.target.value })}>
          {DIALECTS.map((dialect) => <option key={dialect.id} value={dialect.id}>{dialect.label}</option>)}
        </select>
      </div>
      <div className="field">
        <label htmlFor="ds-dsn">DSN</label>
        <input
          id="ds-dsn"
          className="input ab-mono"
          type="password"
          autoComplete="off"
          value={form.dsn}
          placeholder={DIALECTS.find((dialect) => dialect.id === form.dialect)?.placeholder}
          onChange={(e) => edit({ dsn: e.target.value })}
        />
        <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: "5px 0 0", lineHeight: 1.5 }}>
          {t("Point it at a sample or staging database with the same schema, using a read-only account. The DSN is kept in the secret store and never shown again.")}
        </p>
      </div>
      <ErrorNote>{error}</ErrorNote>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>{t("Cancel")}</button>
        <button type="submit" className="btn btn-primary" disabled={busy || !form.name.trim() || !form.dsn.trim()}>
          {busy ? <Spinner size={14} /> : <Check size={14} />}{t("Save and test")}
        </button>
      </div>
    </form>
  );
}

function DatabaseSourceCard({ source, onChanged, onRemove, notify }) {
  const { t } = useLang();
  const [testing, setTesting] = useState(false);
  const [enteringDsn, setEnteringDsn] = useState(!source.has_dsn);
  const [editingAccess, setEditingAccess] = useState(false);

  async function test() {
    setTesting(true);
    try {
      onChanged(await api.databaseSources.test(source.id));
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setTesting(false);
    }
  }

  async function saveDsn(dsn) {
    try {
      onChanged(await api.databaseSources.update(source.id, { dsn }));
      setEnteringDsn(false);
      await test();
    } catch (err) {
      notify(err.message, "error");
    }
  }

  return (
    <div className="card" style={{ gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <Database size={18} style={{ color: "var(--color-accent)", flex: "none" }} />
        <div style={{ flex: "1 1 200px", minWidth: 0 }}>
          <div className="card-title">{source.name}</div>
          <p className="text-muted" style={{ margin: 0, fontSize: "var(--text-xs)" }}>
            {DIALECTS.find((dialect) => dialect.id === source.dialect)?.label || source.dialect}
          </p>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={test} disabled={testing || !source.has_dsn}>
          {testing ? <Spinner size={12} /> : <RefreshCw size={12} />}{t("Test connection")}
        </button>
        {!enteringDsn && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEnteringDsn(true)}>
            <KeyRound size={12} />{t("Re-enter DSN")}
          </button>
        )}
        <button type="button" className="btn btn-ghost btn-icon" title={t("Delete")} aria-label={t("Delete")} onClick={onRemove}>
          <Trash2 size={15} />
        </button>
      </div>

      <ProbeVerdict source={source} />

      {enteringDsn && <DsnForm sourceId={source.id} onSave={saveDsn} onCancel={source.has_dsn ? () => setEnteringDsn(false) : null} />}

      {source.views.length > 0 && <ViewPicker source={source} onChanged={onChanged} notify={notify} />}

      {source.allowed_views.length > 0 && (
        <div style={{ borderTop: "1px solid var(--color-divider)", paddingTop: 10 }}>
          <button type="button" className="btn btn-ghost btn-sm" aria-expanded={editingAccess} onClick={() => setEditingAccess(!editingAccess)}>
            <ShieldCheck size={12} />{t("Access rules")}
          </button>
          {editingAccess && (
            <div style={{ marginTop: 10 }}>
              <AccessRulesEditor source={source} notify={notify} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The last probe's verdict, in the backend's own sentence — it already names the fix and never quotes the DSN. */
function ProbeVerdict({ source }) {
  const { t } = useLang();
  if (source.status === "untested") {
    return <p className="text-muted" style={{ margin: 0, fontSize: "var(--text-sm)" }}>{t("Not tested yet.")}</p>;
  }
  const ok = source.status === "connected";
  return (
    <p style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.55, color: ok ? "var(--color-accent)" : "#a12626" }}>
      {ok ? <Check size={15} style={{ flexShrink: 0, marginTop: 2 }} /> : <X size={15} style={{ flexShrink: 0, marginTop: 2 }} />}
      <span>{source.status_detail}</span>
    </p>
  );
}

function DsnForm({ sourceId, onSave, onCancel }) {
  const { t } = useLang();
  const [dsn, setDsn] = useState("");
  const id = `ds-new-dsn-${sourceId}`;

  return (
    <form
      onSubmit={(event) => { event.preventDefault(); if (dsn.trim()) onSave(dsn.trim()); }}
      style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}
    >
      <div className="field" style={{ flex: "1 1 260px", margin: 0 }}>
        <label htmlFor={id}>{t("New DSN")}</label>
        <input id={id} className="input ab-mono" type="password" autoComplete="off" value={dsn} onChange={(e) => setDsn(e.target.value)} />
      </div>
      <button type="submit" className="btn btn-primary btn-sm" disabled={!dsn.trim()}>{t("Save DSN")}</button>
      {onCancel && <button type="button" className="btn btn-ghost btn-sm" onClick={onCancel}>{t("Cancel")}</button>}
    </form>
  );
}

/** One checkbox per view the last probe saw; base tables are named but never offered. */
function ViewPicker({ source, onChanged, notify }) {
  const { t } = useLang();
  const [ticked, setTicked] = useState(() => new Set(source.allowed_views));
  const [saving, setSaving] = useState(false);

  useEffect(() => { setTicked(new Set(source.allowed_views)); }, [source.allowed_views]);

  const toggle = (name) =>
    setTicked((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const unchanged = ticked.size === source.allowed_views.length && source.allowed_views.every((name) => ticked.has(name));

  async function save() {
    setSaving(true);
    try {
      // In the order the probe listed them, not the order they were clicked.
      const tickedInListOrder = source.views.map((view) => view.name).filter((name) => ticked.has(name));
      onChanged(await api.databaseSources.update(source.id, { allowed_views: tickedInListOrder }));
      notify(t("Allowed views saved"));
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={{ borderTop: "1px solid var(--color-divider)", paddingTop: 10 }}>
      <p className="text-muted" style={{ margin: "0 0 8px", fontSize: "var(--text-xs)" }}>
        {t("Tick the views an agent may read. Only views can be allowed — never base tables.")}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {source.views.map((view) => (
          <label key={view.name} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: "var(--text-sm)" }}>
            <input type="checkbox" checked={ticked.has(view.name)} onChange={() => toggle(view.name)} />
            <span className="ab-mono" style={{ flex: 1, minWidth: 0 }}>{view.name}</span>
            <span className="text-muted" style={{ fontSize: "var(--text-xs)" }}>
              {view.row_count === null
                ? t("rows not counted · {columns} columns", { columns: view.columns.length })
                : t("{rows} rows · {columns} columns", { rows: view.row_count.toLocaleString(), columns: view.columns.length })}
            </span>
          </label>
        ))}
      </div>
      {source.base_tables.length > 0 && (
        <p className="text-muted" style={{ margin: "8px 0 0", fontSize: "var(--text-xs)" }}>
          {t("Base tables (cannot be allowed): {names}", { names: source.base_tables.join(", ") })}
        </p>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
        <button type="button" className="btn btn-secondary btn-sm" onClick={save} disabled={saving || unchanged}>
          {saving ? <Spinner size={12} /> : <Check size={12} />}{t("Save allowed views")}
        </button>
      </div>
    </div>
  );
}
