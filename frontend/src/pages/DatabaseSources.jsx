import { useCallback, useEffect, useState } from "react";
import { Check, Database, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import { useLang } from "../i18n";
import { AgentMascot } from "../components/mascot";
import AccessRulesEditor from "./AccessRulesEditor";

/* ============================================================
   The Databases tab of "Nguồn dữ liệu" (DS-02 / ADR-0059 §2): register a Hệ
   CSDL, test it, see its views with their row counts, tick the ones an agent
   may read.

   A connection is entered field by field (host, port, database, user,
   password) or, for what the fields cannot say — a SQL Server named
   instance, SSL options — as one connection string. Either way the browser
   sends a single DSN: the fields are composed into the URL every dialect's
   parser already takes, so the API has one shape to guard. Nothing typed is
   ever shown again — the API only answers `has_dsn`.

   A base table is listed, so a creator can see it was found, but it has no
   checkbox: ADR-0059 lets an agent read views only, and the API refuses one
   ticked anyway.
   ============================================================ */

//: What the dialect picker offers. `scheme`/`port` are what the field-by-field
//: form composes its URL with; `placeholder` is the connection-string shape.
//: `sessionAttributes: false` is MySQL's missing defence (ADR-0059 §8): the
//: asker's attributes never reach the database session, so a customer's own
//: row-level security has nothing to read — said beside every MySQL source.
const DIALECTS = [
  { id: "postgresql", label: "PostgreSQL", scheme: "postgresql", port: "5432", placeholder: "postgresql://user:password@host:5432/erp", sessionAttributes: true },
  { id: "sqlserver", label: "SQL Server", scheme: "mssql", port: "1433", placeholder: "Server=host,1433;Database=erp;User Id=user;Password=password", sessionAttributes: true },
  { id: "mysql", label: "MySQL", scheme: "mysql", port: "3306", placeholder: "mysql://user:password@host:3306/erp", sessionAttributes: false },
];

const dialectOf = (id) => DIALECTS.find((dialect) => dialect.id === id);

const EMPTY_CONNECTION = { mode: "fields", host: "", port: "", database: "", user: "", password: "", dsn: "" };

/**
 * The DSN a connection form stands for, or "" while it cannot be one yet.
 *
 * Every part a person typed is percent-encoded, so a password holding `@`,
 * `/` or `:` reaches the server as typed rather than splitting the URL. An
 * empty port is the dialect's default; a bare IPv6 address gets its brackets.
 */
export function dsnOf(dialectId, connection) {
  if (connection.mode === "dsn") return connection.dsn.trim();
  const dialect = dialectOf(dialectId);
  const host = connection.host.trim();
  const user = connection.user.trim();
  const database = connection.database.trim();
  const port = connection.port.trim() || dialect?.port;
  if (!dialect || !host || !user || !database || !/^\d{1,5}$/.test(port)) return "";
  const address = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  const secret = connection.password ? `:${encodeURIComponent(connection.password)}` : "";
  return `${dialect.scheme}://${encodeURIComponent(user)}${secret}@${address}:${port}/${encodeURIComponent(database)}`;
}

/** The note a dialect without session attributes carries wherever it is shown. */
function CodeOnlyScopeNote({ dialect }) {
  const { t } = useLang();
  if (dialectOf(dialect)?.sessionAttributes !== false) return null;
  return (
    <p className="text-muted" role="note" style={{ fontSize: "var(--text-xs)", margin: "5px 0 0", lineHeight: 1.5, display: "flex", gap: 6 }}>
      <ShieldCheck size={13} style={{ flex: "none", marginTop: 1 }} />
      {t("MySQL has no session attributes: the asker's scope is filtered by the platform's code only — no second layer in the database.")}
    </p>
  );
}

/** Field by field, or one connection string — both end as one DSN (`dsnOf`). */
function ConnectionFields({ idPrefix, dialect, value, onChange }) {
  const { t } = useLang();
  const edit = (fields) => onChange({ ...value, ...fields });
  const input = (key, label, props = {}) => (
    <div className="field" style={{ margin: 0, minWidth: 0 }}>
      <label htmlFor={`${idPrefix}-${key}`}>{label}</label>
      <input
        id={`${idPrefix}-${key}`}
        className="input"
        autoComplete="off"
        value={value[key]}
        onChange={(e) => edit({ [key]: e.target.value })}
        {...props}
      />
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="seg" role="radiogroup" aria-label={t("How to enter the connection")} style={{ alignSelf: "flex-start" }}>
        <label className="seg-opt">
          <input type="radio" name={`${idPrefix}-mode`} checked={value.mode === "fields"} onChange={() => edit({ mode: "fields" })} />
          {t("Field by field")}
        </label>
        <label className="seg-opt">
          <input type="radio" name={`${idPrefix}-mode`} checked={value.mode === "dsn"} onChange={() => edit({ mode: "dsn" })} />
          {t("Connection string")}
        </label>
      </div>

      {value.mode === "fields" ? (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 96px", gap: 10 }}>
            {input("host", t("Host"), { placeholder: "erp.congty.local" })}
            {input("port", t("Port"), { inputMode: "numeric", placeholder: dialectOf(dialect)?.port })}
          </div>
          {input("database", t("Database name"), { placeholder: "erp" })}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
            {input("user", t("Username"), { placeholder: "readonly_user" })}
            {input("password", t("Password"), { type: "password" })}
          </div>
          <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: 1.5 }}>
            {t("A SQL Server named instance or extra options such as SSL? Use the connection string instead.")}
          </p>
        </>
      ) : (
        input("dsn", t("Connection string (DSN)"), { type: "password", className: "input ab-mono", placeholder: dialectOf(dialect)?.placeholder })
      )}
    </div>
  );
}

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
  const [name, setName] = useState("");
  const [dialect, setDialect] = useState("postgresql");
  const [connection, setConnection] = useState(EMPTY_CONNECTION);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dsn = dsnOf(dialect, connection);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onCreated(await api.databaseSources.create({ name: name.trim(), dialect, dsn }));
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
        <input id="ds-name" className="input" value={name} placeholder="ERP staging" onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label id="ds-dialect-label">{t("Database type")}</label>
        <div className="seg" role="radiogroup" aria-labelledby="ds-dialect-label" style={{ alignSelf: "flex-start" }}>
          {DIALECTS.map((option) => (
            <label key={option.id} className="seg-opt">
              <input type="radio" name="ds-dialect" checked={dialect === option.id} onChange={() => setDialect(option.id)} />
              <Database size={13} />{option.label}
            </label>
          ))}
        </div>
        <CodeOnlyScopeNote dialect={dialect} />
      </div>
      <ConnectionFields idPrefix="ds-new" dialect={dialect} value={connection} onChange={setConnection} />
      <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: 1.5, display: "flex", gap: 6 }}>
        <KeyRound size={13} style={{ flex: "none", marginTop: 1 }} />
        {t("Point it at a sample or staging database with the same schema, using a read-only account. The connection is encrypted in the secret store and never shown again.")}
      </p>
      <ErrorNote>{error}</ErrorNote>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>{t("Cancel")}</button>
        <button type="submit" className="btn btn-primary" disabled={busy || !name.trim() || !dsn}>
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
            {dialectOf(source.dialect)?.label || source.dialect}
          </p>
          <CodeOnlyScopeNote dialect={source.dialect} />
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={test} disabled={testing || !source.has_dsn}>
          {testing ? <Spinner size={12} /> : <RefreshCw size={12} />}{t("Test connection")}
        </button>
        {!enteringDsn && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEnteringDsn(true)}>
            <KeyRound size={12} />{t("Re-enter connection")}
          </button>
        )}
        <button type="button" className="btn btn-ghost btn-icon" title={t("Delete")} aria-label={t("Delete")} onClick={onRemove}>
          <Trash2 size={15} />
        </button>
      </div>

      <ProbeVerdict source={source} />

      {enteringDsn && <ReconnectForm source={source} onSave={saveDsn} onCancel={source.has_dsn ? () => setEnteringDsn(false) : null} />}

      {source.views.length > 0 && <ViewPicker source={source} onChanged={onChanged} notify={notify} />}

      {source.status === "connected" && <WriteTablePicker source={source} onChanged={onChanged} notify={notify} />}

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

/** A new connection for a source whose own is missing or being replaced — the same fields as when adding one. */
function ReconnectForm({ source, onSave, onCancel }) {
  const { t } = useLang();
  const [connection, setConnection] = useState(EMPTY_CONNECTION);
  const dsn = dsnOf(source.dialect, connection);

  return (
    <form
      onSubmit={(event) => { event.preventDefault(); if (dsn) onSave(dsn); }}
      style={{ display: "flex", flexDirection: "column", gap: 10, borderTop: "1px solid var(--color-divider)", paddingTop: 10 }}
    >
      <ConnectionFields idPrefix={`ds-${source.id}`} dialect={source.dialect} value={connection} onChange={setConnection} />
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        {onCancel && <button type="button" className="btn btn-ghost btn-sm" onClick={onCancel}>{t("Cancel")}</button>}
        <button type="submit" className="btn btn-primary btn-sm" disabled={!dsn}>
          <Check size={12} />{t("Save and test")}
        </button>
      </div>
    </form>
  );
}

/** One checkbox per base table the last probe saw with its columns: what a `data_write` node may change (ADR-0094). */
function WriteTablePicker({ source, onChanged, notify }) {
  const { t } = useLang();
  const tables = source.tables || [];
  const [ticked, setTicked] = useState(() => new Set(source.allowed_write_tables || []));
  const [saving, setSaving] = useState(false);

  useEffect(() => { setTicked(new Set(source.allowed_write_tables || [])); }, [source.allowed_write_tables]);

  const saved = source.allowed_write_tables || [];
  const unchanged = ticked.size === saved.length && saved.every((name) => ticked.has(name));
  const toggle = (name) =>
    setTicked((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  async function save() {
    setSaving(true);
    try {
      const inListOrder = tables.map((table) => table.name).filter((name) => ticked.has(name));
      onChanged(await api.databaseSources.update(source.id, { allowed_write_tables: inListOrder }));
      notify(t("Writable tables saved"));
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={{ borderTop: "1px solid var(--color-divider)", paddingTop: 10 }}>
      <p style={{ margin: "0 0 4px", fontSize: "var(--text-sm)", fontWeight: 600 }}>{t("Tables an agent may change")}</p>
      {tables.length === 0 ? (
        <p className="text-muted" style={{ margin: 0, fontSize: "var(--text-xs)" }}>
          {source.dialect === "postgresql"
            ? t("No base table found on this database.")
            : t("Changing data is supported on PostgreSQL only for now.")}
        </p>
      ) : (
        <>
          <p className="text-muted" role="note" style={{ margin: "0 0 8px", fontSize: "var(--text-xs)", display: "flex", gap: 6 }}>
            <ShieldCheck size={13} style={{ flex: "none", marginTop: 1 }} />
            {t("An agent adds, edits or deletes rows only in ticked tables, only as the access rules allow, and only after a person approves the exact change.")}
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {tables.map((table) => (
              <label key={table.name} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: "var(--text-sm)" }}>
                <input type="checkbox" checked={ticked.has(table.name)} onChange={() => toggle(table.name)} />
                <span className="ab-mono" style={{ flex: 1, minWidth: 0 }}>{table.name}</span>
                <span className="text-muted" style={{ fontSize: "var(--text-xs)" }}>{t("{columns} columns", { columns: table.columns.length })}</span>
              </label>
            ))}
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
            <button type="button" className="btn btn-secondary btn-sm" onClick={save} disabled={saving || unchanged}>
              {saving ? <Spinner size={12} /> : <Check size={12} />}{t("Save writable tables")}
            </button>
          </div>
        </>
      )}
    </div>
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
          {t("Base tables (cannot be read directly): {names}", { names: source.base_tables.join(", ") })}
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
