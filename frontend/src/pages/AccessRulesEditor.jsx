import { useEffect, useState } from "react";
import { Check, FileCode, Plus, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import * as api from "../lib/api";
import { Spinner } from "../components/ui";
import { useLang } from "../i18n";

/* ============================================================
   Quy tắc truy cập for one Hệ CSDL (DS-03 / ADR-0059 §6): who the asker is
   (`identity`), and per position × view whether they see nothing, everything,
   or only the rows where a view column equals one of their own attributes.

   The form edits a draft shaped for editing — attributes as rows, a grant per
   cell — and `toRules` turns it into exactly the `access.yaml` model the API
   checks. "Deny" is never written down: a position whose every cell is deny
   has no rule, which is what deny-by-default means.

   Writing (ADR-0094) is a second matrix over the tables ticked for writing:
   which of add / edit / delete a position may do, and on which rows — the
   same filter of `table column = asker attribute`, or every row. Nothing in
   it is implied by the first matrix: seeing a view never grants changing it.
   ============================================================ */

const POSITION = "position";

const GRANTS = [
  { id: "deny", label: "Deny" },
  { id: "all", label: "Everything" },
  { id: "where", label: "Filter" },
];

const OPERATIONS = [
  { id: "insert", label: "Add" },
  { id: "update", label: "Edit" },
  { id: "delete", label: "Delete" },
];

//: Column names a guess recognises (ADR-0095), lower-cased. Only a starting point:
//: the creator sees every guess filled into the form and can change it.
const EMAIL_COLUMNS = ["email", "e_mail", "mail", "work_email", "email_address"];
const POSITION_COLUMNS = ["position", "chuc_vu", "chucvu", "role", "job_title", "title", "vai_tro", "chuc_danh"];
const KNOWN_ATTRIBUTES = [
  { name: "employee_code", columns: ["employee_code", "emp_id", "employee_id", "staff_code", "ma_nv", "ma_nhan_vien"] },
  { name: "department", columns: ["department", "dept", "dept_code", "department_code", "phong_ban", "bo_phan"] },
];

const pick = (columns, names) => columns.find((column) => names.includes(column.toLowerCase())) || "";

/** The employee view and its attributes, guessed from column names — the form's starting point, never saved unseen. */
export function suggestIdentity(source) {
  const views = source.allowed_views.map((name) => ({ name, columns: source.views.find((entry) => entry.name === name)?.columns || [] }));
  const employees = views.find((view) => pick(view.columns, EMAIL_COLUMNS) && pick(view.columns, POSITION_COLUMNS));
  if (!employees) return { view: source.allowed_views[0] || "", matchBy: "", attributes: [{ name: POSITION, column: "" }] };
  const attributes = [{ name: POSITION, column: pick(employees.columns, POSITION_COLUMNS) }];
  for (const known of KNOWN_ATTRIBUTES) {
    const column = pick(employees.columns, known.columns);
    if (column) attributes.push({ name: known.name, column });
  }
  return { view: employees.name, matchBy: pick(employees.columns, EMAIL_COLUMNS), attributes };
}

const emptyDraft = (source) => ({
  ...suggestIdentity(source),
  positions: [],
  grants: {},
  tableGrants: {},
});

/** The saved model → the editable draft. */
function draftOf(rules) {
  const grants = {};
  for (const rule of rules.rules) {
    grants[rule.position] = Object.fromEntries(
      // A rule that only writes comes back with no `views` at all (ADR-0094).
      Object.entries(rule.views || {}).map(([view, grant]) => [
        view,
        grant === "all"
          ? { mode: "all", pairs: [] }
          : { mode: "where", pairs: Object.entries(grant.where).map(([column, attribute]) => ({ column, attribute })) },
      ]),
    );
  }
  const tableGrants = {};
  for (const rule of rules.rules) {
    tableGrants[rule.position] = Object.fromEntries(
      Object.entries(rule.tables || {}).map(([table, grant]) => [
        table,
        {
          ops: grant.operations,
          mode: grant.all_rows ? "all" : "where",
          pairs: Object.entries(grant.where || {}).map(([column, attribute]) => ({ column, attribute })),
        },
      ]),
    );
  }
  return {
    view: rules.identity.view,
    matchBy: rules.identity.match_by,
    attributes: Object.entries(rules.identity.attributes).map(([name, column]) => ({ name, column })),
    positions: rules.rules.map((rule) => rule.position),
    grants,
    tableGrants,
  };
}

/** One position's write grant on one table as the API's `TableGrant`, or `null` for none (no operation, or no complete filter). */
function tableGrantOf(grant) {
  if (!grant || grant.ops.length === 0) return null;
  if (grant.mode === "all") return { operations: grant.ops, all_rows: true };
  const where = Object.fromEntries(
    grant.pairs.filter((pair) => pair.column && pair.attribute).map((pair) => [pair.column, pair.attribute]),
  );
  return Object.keys(where).length ? { operations: grant.ops, where } : null;
}

/** The draft → the `access.yaml` model. Deny cells and incomplete filter pairs are simply not written. */
function toRules(draft, grantViews, grantTables = []) {
  const attributes = Object.fromEntries(
    draft.attributes.filter((row) => row.name.trim() && row.column).map((row) => [row.name.trim(), row.column]),
  );
  const rules = draft.positions
    .map((position) => {
      const views = {};
      for (const view of grantViews) {
        const grant = draft.grants[position]?.[view];
        if (grant?.mode === "all") views[view] = "all";
        if (grant?.mode === "where") {
          const where = Object.fromEntries(
            grant.pairs.filter((pair) => pair.column && pair.attribute).map((pair) => [pair.column, pair.attribute]),
          );
          if (Object.keys(where).length) views[view] = { where };
        }
      }
      const tables = {};
      for (const table of grantTables) {
        const grant = tableGrantOf(draft.tableGrants?.[position]?.[table]);
        if (grant) tables[table] = grant;
      }
      return Object.keys(tables).length ? { position, views, tables } : { position, views };
    })
    .filter((rule) => Object.keys(rule.views).length > 0 || Object.keys(rule.tables || {}).length > 0);
  return { identity: { view: draft.view, match_by: draft.matchBy, attributes }, rules };
}

/** Every `[position, view or table]` set to Filter with no complete pair. `toRules` would drop it — a deny nobody chose. */
function incompleteFilters(draft, grantViews, grantTables = []) {
  const complete = (grant) => grant.pairs.some((pair) => pair.column && pair.attribute);
  const views = draft.positions.flatMap((position) =>
    grantViews
      .filter((view) => {
        const grant = draft.grants[position]?.[view];
        return grant?.mode === "where" && !complete(grant);
      })
      .map((view) => [position, view]),
  );
  const tables = draft.positions.flatMap((position) =>
    grantTables
      .filter((table) => {
        const grant = draft.tableGrants?.[position]?.[table];
        return grant && grant.ops.length > 0 && grant.mode === "where" && !complete(grant);
      })
      .map((table) => [position, table]),
  );
  return [...views, ...tables];
}

/** A refusal's problems, whichever of the API's two 422 shapes it came in. */
function problemsOf(err) {
  const detail = err.body && err.body.detail;
  if (detail && Array.isArray(detail.problems)) return detail.problems;
  if (Array.isArray(detail)) return detail.map((error) => `${(error.loc || []).join(".")}: ${error.msg}`);
  return null;
}

export default function AccessRulesEditor({ source, notify }) {
  const { t } = useLang();
  const [draft, setDraft] = useState(() => emptyDraft(source));
  const [problems, setProblems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [yamlText, setYamlText] = useState(null);
  // DS-12: the creator's words, and what the last draft from them cut out. `drafted`
  // stays true until the rules are saved — a draft on the form is not a rule yet.
  const [description, setDescription] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [drafted, setDrafted] = useState(false);
  const [dropped, setDropped] = useState([]);
  // ADR-0095: "open" — everyone sees the same, no employee view, no rules.
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.databaseSources
      .accessRules(source.id)
      .then((saved) => {
        if (cancelled) return;
        if (saved.rules?.open) setOpen(true);
        else if (saved.rules) setDraft(draftOf(saved.rules));
        setProblems(saved.problems || []);
      })
      .catch((err) => notify(err.message, "error"))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [source.id, notify]);

  const columnsOf = (view) => source.views.find((entry) => entry.name === view)?.columns || [];
  const grantViews = source.allowed_views.filter((view) => view !== draft.view);
  const grantTables = source.allowed_write_tables || [];
  const tableColumnsOf = (table) => (source.tables || []).find((entry) => entry.name === table)?.columns || [];
  const attributeNames = draft.attributes.map((row) => row.name.trim()).filter(Boolean);
  const positionColumn = draft.attributes.find((row) => row.name === POSITION)?.column;
  const edit = (fields) => setDraft((current) => ({ ...current, ...fields }));

  function setGrant(position, view, grant) {
    setDraft((current) => ({
      ...current,
      grants: { ...current.grants, [position]: { ...current.grants[position], [view]: grant } },
    }));
  }

  function setTableGrant(position, table, grant) {
    setDraft((current) => ({
      ...current,
      tableGrants: { ...current.tableGrants, [position]: { ...current.tableGrants?.[position], [table]: grant } },
    }));
  }

  async function loadPositions() {
    try {
      const { values } = await api.databaseSources.distinctValues(source.id, draft.view, positionColumn);
      setDraft((current) => ({
        ...current,
        positions: [...current.positions, ...values.map(String).filter((value) => !current.positions.includes(value))],
      }));
    } catch (err) {
      notify(err.message, "error");
    }
  }

  async function suggest() {
    setDrafting(true);
    try {
      const result = await api.databaseSources.draftAccessRules(source.id, description.trim(), toRules(draft, grantViews, grantTables).identity);
      const proposed = draftOf(result.rules);
      // Positions already on the form stay — a position the draft has no rule for is simply denied.
      // A draft that proposes write grants replaces the form's; one that proposes none leaves them as they are.
      const proposesWrites = Object.values(proposed.tableGrants).some((tables) => Object.keys(tables).length > 0);
      setDraft((current) => ({
        ...proposed,
        positions: [...proposed.positions, ...current.positions.filter((position) => !proposed.positions.includes(position))],
        tableGrants: proposesWrites ? proposed.tableGrants : current.tableGrants,
      }));
      setDropped(result.dropped || []);
      setProblems(result.problems || []);
      setDrafted(true);
    } catch (err) {
      const refused = problemsOf(err);
      if (refused) setProblems(refused);
      else notify(err.message, "error");
    } finally {
      setDrafting(false);
    }
  }

  async function saveOpen() {
    setSaving(true);
    try {
      const saved = await api.databaseSources.saveAccessRules(source.id, { open: true });
      setProblems(saved.problems || []);
      setYamlText(null);
      notify(t("Access rules saved"));
    } catch (err) {
      const refused = problemsOf(err);
      if (refused) setProblems(refused);
      else notify(err.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function save() {
    const incomplete = incompleteFilters(draft, grantViews, grantTables).map(([position, view]) =>
      t("{position} — {view}: the filter has no complete condition. Pick a view column and an asker attribute, or choose Deny.", { position, view }),
    );
    if (incomplete.length) {
      setProblems(incomplete);
      return;
    }
    setSaving(true);
    try {
      const saved = await api.databaseSources.saveAccessRules(source.id, toRules(draft, grantViews, grantTables));
      setProblems(saved.problems || []);
      setYamlText(null);
      setDrafted(false);
      setDropped([]);
      notify(t("Access rules saved"));
    } catch (err) {
      const refused = problemsOf(err);
      if (refused) setProblems(refused);
      else notify(err.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function showYaml() {
    try {
      setYamlText(await api.databaseSources.accessRulesYaml(source.id));
    } catch (err) {
      notify(err.status === 404 ? t("Save the rules first to see them as YAML.") : err.message, "error");
    }
  }

  if (loading) return <Spinner size={14} />;

  const nothingGranted = toRules(draft, grantViews, grantTables).rules.length === 0;

  const modePicker = (
    <div className="seg" role="radiogroup" aria-label={t("Who may use this data")} style={{ alignSelf: "flex-start" }}>
      <label className="seg-opt">
        <input type="radio" name={`ar-mode-${source.id}`} checked={open} onChange={() => setOpen(true)} />
        {t("Everyone the same")}
      </label>
      <label className="seg-opt">
        <input type="radio" name={`ar-mode-${source.id}`} checked={!open} onChange={() => setOpen(false)} />
        {t("By position")}
      </label>
    </div>
  );

  if (open) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {modePicker}
        <p className="text-muted" style={{ margin: 0, fontSize: "var(--text-sm)", lineHeight: 1.5 }}>
          {t("Everyone who uses the agent reads every ticked view, and may change every table ticked for writing — each change still waits for a person to approve it. For a database with nothing to keep apart.")}
        </p>
        {problems.length > 0 && (
          <ul style={{ margin: 0, paddingLeft: 18, color: "#a12626", fontSize: "var(--text-sm)" }}>
            {problems.map((problem) => <li key={problem}>{problem}</li>)}
          </ul>
        )}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button type="button" className="btn btn-ghost btn-sm" onClick={showYaml}>
            <FileCode size={12} />{t("View YAML")}
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={saveOpen} disabled={saving}>
            {saving ? <Spinner size={12} /> : <Check size={12} />}{t("Save access rules")}
          </button>
        </div>
        {yamlText !== null && (
          <pre className="ab-mono" style={{ margin: 0, padding: "var(--space-2)", fontSize: "var(--text-xs)", border: "1px solid var(--color-divider)", whiteSpace: "pre-wrap" }}>
            {yamlText}
          </pre>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {modePicker}
      <IdentityFields draft={draft} source={source} columnsOf={columnsOf} edit={edit} />

      <div className="field">
        <label htmlFor={`ar-describe-${source.id}`}>{t("Describe who sees what")}</label>
        <textarea
          id={`ar-describe-${source.id}`}
          className="input"
          rows={2}
          value={description}
          placeholder={t("Sales reps see only the customers they handle; team leads see their whole department.")}
          onChange={(event) => setDescription(event.target.value)}
        />
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 6 }}>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={suggest}
            disabled={drafting || !description.trim() || !draft.view || !draft.matchBy || !positionColumn}
          >
            {drafting ? <Spinner size={12} /> : <Sparkles size={12} />}{t("Suggest rules")}
          </button>
        </div>
      </div>

      {drafted && (
        <div role="status" style={{ border: "1px solid var(--color-accent)", background: "var(--color-accent-100)", color: "var(--color-accent-800)", padding: "var(--space-2) var(--space-3)", fontSize: "var(--text-sm)" }}>
          <strong>{t("Draft — not saved.")}</strong> {t("Review every cell below, change what is wrong, then save. Nothing applies until you do.")}
          {dropped.length > 0 && (
            <>
              <div style={{ marginTop: 6 }}>{t("Left out because the database does not have it:")}</div>
              <ul data-testid="draft-dropped" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {dropped.map((line) => <li key={line}>{line}</li>)}
              </ul>
            </>
          )}
        </div>
      )}

      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <strong style={{ fontSize: "var(--text-sm)" }}>{t("Who sees what")}</strong>
          <button type="button" className="btn btn-ghost btn-sm" onClick={loadPositions} disabled={!draft.view || !positionColumn}>
            <RefreshCw size={12} />{t("Load positions")}
          </button>
        </div>
        {nothingGranted && (
          <p className="text-muted" style={{ margin: "0 0 6px", fontSize: "var(--text-xs)" }}>
            {t("Nobody can read anything yet — every asker is denied until a rule says otherwise.")}
          </p>
        )}
        {draft.positions.length > 0 && grantViews.length > 0 && (
          <GrantMatrix
            draft={draft}
            grantViews={grantViews}
            columnsOf={columnsOf}
            attributeNames={attributeNames}
            setGrant={setGrant}
          />
        )}
      </div>

      {grantTables.length > 0 && draft.positions.length > 0 && (
        <div>
          <strong style={{ fontSize: "var(--text-sm)", display: "block", marginBottom: 4 }}>{t("Who may change what")}</strong>
          <p className="text-muted" style={{ margin: "0 0 6px", fontSize: "var(--text-xs)" }}>
            {t("Every change an agent makes waits for a person to approve the exact change first. Nobody may change anything unless ticked here.")}
          </p>
          <WriteGrantMatrix
            draft={draft}
            grantTables={grantTables}
            columnsOf={tableColumnsOf}
            attributeNames={attributeNames}
            setTableGrant={setTableGrant}
          />
        </div>
      )}

      {problems.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: 18, color: "#a12626", fontSize: "var(--text-sm)" }}>
          {problems.map((problem) => <li key={problem}>{problem}</li>)}
        </ul>
      )}

      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button type="button" className="btn btn-ghost btn-sm" onClick={showYaml}>
          <FileCode size={12} />{t("View YAML")}
        </button>
        <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={saving || !draft.view || !draft.matchBy}>
          {saving ? <Spinner size={12} /> : <Check size={12} />}{t("Save access rules")}
        </button>
      </div>

      {yamlText !== null && (
        <pre className="ab-mono" style={{ margin: 0, padding: "var(--space-2)", fontSize: "var(--text-xs)", border: "1px solid var(--color-divider)", whiteSpace: "pre-wrap" }}>
          {yamlText}
        </pre>
      )}
    </div>
  );
}

/** Which view is the employee view, which column matches the signed-in value, which columns to read off it. */
function IdentityFields({ draft, source, columnsOf, edit }) {
  const { t } = useLang();
  const columns = columnsOf(draft.view);
  const setAttribute = (index, fields) =>
    edit({ attributes: draft.attributes.map((row, position) => (position === index ? { ...row, ...fields } : row)) });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <label className="field" style={{ flex: "1 1 180px", margin: 0 }}>
          <span>{t("Employee view")}</span>
          <select className="input" value={draft.view} onChange={(e) => edit({ view: e.target.value })}>
            {source.allowed_views.map((view) => <option key={view} value={view}>{view}</option>)}
          </select>
        </label>
        <label className="field" style={{ flex: "1 1 180px", margin: 0 }}>
          <span>{t("Matched against the sign-in by")}</span>
          <select className="input" value={draft.matchBy} onChange={(e) => edit({ matchBy: e.target.value })}>
            <option value="">—</option>
            {columns.map((column) => <option key={column} value={column}>{column}</option>)}
          </select>
        </label>
      </div>
      <p className="text-muted" style={{ margin: 0, fontSize: "var(--text-xs)" }}>
        {t("Attributes read off the asker's row. “position” is required — rules are written per position.")}
      </p>
      {draft.attributes.map((row, index) => (
        <div key={index} style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            className="input ab-mono"
            style={{ flex: "1 1 140px", minWidth: 0 }}
            aria-label={t("Attribute name")}
            value={row.name}
            disabled={row.name === POSITION && index === draft.attributes.findIndex((entry) => entry.name === POSITION)}
            onChange={(e) => setAttribute(index, { name: e.target.value })}
          />
          <select
            className="input"
            style={{ flex: "1 1 140px", minWidth: 0 }}
            aria-label={t("Column for {name}", { name: row.name || "…" })}
            value={row.column}
            onChange={(e) => setAttribute(index, { column: e.target.value })}
          >
            <option value="">—</option>
            {columns.map((column) => <option key={column} value={column}>{column}</option>)}
          </select>
          {row.name !== POSITION && (
            <button
              type="button"
              className="btn btn-ghost btn-icon"
              aria-label={t("Remove")}
              onClick={() => edit({ attributes: draft.attributes.filter((_, position) => position !== index) })}
            >
              <Trash2 size={13} />
            </button>
          )}
        </div>
      ))}
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        style={{ alignSelf: "flex-start" }}
        onClick={() => edit({ attributes: [...draft.attributes, { name: "", column: "" }] })}
      >
        <Plus size={12} />{t("Add an attribute")}
      </button>
    </div>
  );
}

/** One row per position, one column per view: deny, everything, or a filter of `view column = asker attribute` pairs. */
function GrantMatrix({ draft, grantViews, columnsOf, attributeNames, setGrant }) {
  const { t } = useLang();

  return (
    <div style={{ overflowX: "auto" }}>
      <table className="table" style={{ fontSize: "var(--text-sm)" }}>
        <thead>
          <tr>
            <th>{t("Position")}</th>
            {grantViews.map((view) => <th key={view} className="ab-mono">{view}</th>)}
          </tr>
        </thead>
        <tbody>
          {draft.positions.map((position) => (
            <tr key={position}>
              <td>{position}</td>
              {grantViews.map((view) => {
                const grant = draft.grants[position]?.[view] || { mode: "deny", pairs: [] };
                return (
                  <td key={view} style={{ verticalAlign: "top" }}>
                    <select
                      className="input"
                      aria-label={`${position} — ${view}`}
                      value={grant.mode}
                      onChange={(e) =>
                        setGrant(position, view, {
                          mode: e.target.value,
                          pairs: e.target.value === "where" && grant.pairs.length === 0 ? [{ column: "", attribute: "" }] : grant.pairs,
                        })
                      }
                    >
                      {GRANTS.map((entry) => <option key={entry.id} value={entry.id}>{t(entry.label)}</option>)}
                    </select>
                    {grant.mode === "where" && (
                      <WherePairs
                        testId={`where-${position}-${view}`}
                        pairs={grant.pairs}
                        columns={columnsOf(view)}
                        attributeNames={attributeNames}
                        onChange={(pairs) => setGrant(position, view, { mode: "where", pairs })}
                      />
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One row per position, one column per writable table: which of add / edit / delete, and on which rows. */
function WriteGrantMatrix({ draft, grantTables, columnsOf, attributeNames, setTableGrant }) {
  const { t } = useLang();

  return (
    <div style={{ overflowX: "auto" }}>
      <table className="table" style={{ fontSize: "var(--text-sm)" }}>
        <thead>
          <tr>
            <th>{t("Position")}</th>
            {grantTables.map((table) => <th key={table} className="ab-mono">{table}</th>)}
          </tr>
        </thead>
        <tbody>
          {draft.positions.map((position) => (
            <tr key={position}>
              <td>{position}</td>
              {grantTables.map((table) => {
                const grant = draft.tableGrants?.[position]?.[table] || { ops: [], mode: "where", pairs: [] };
                const toggle = (op) => {
                  const ops = grant.ops.includes(op) ? grant.ops.filter((entry) => entry !== op) : [...grant.ops, op];
                  const pairs = grant.mode === "where" && grant.pairs.length === 0 ? [{ column: "", attribute: "" }] : grant.pairs;
                  setTableGrant(position, table, { ...grant, ops: OPERATIONS.map((entry) => entry.id).filter((id) => ops.includes(id)), pairs });
                };
                return (
                  <td key={table} style={{ verticalAlign: "top" }} data-testid={`write-${position}-${table}`}>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      {OPERATIONS.map((op) => (
                        <label key={op.id} style={{ display: "flex", gap: 4, alignItems: "center" }}>
                          <input
                            type="checkbox"
                            aria-label={`${position} — ${table} — ${t(op.label)}`}
                            checked={grant.ops.includes(op.id)}
                            onChange={() => toggle(op.id)}
                          />
                          {t(op.label)}
                        </label>
                      ))}
                    </div>
                    {grant.ops.length > 0 && (
                      <>
                        <select
                          className="input"
                          style={{ marginTop: 4 }}
                          aria-label={`${position} — ${table} — ${t("Rows")}`}
                          value={grant.mode}
                          onChange={(e) =>
                            setTableGrant(position, table, {
                              ...grant,
                              mode: e.target.value,
                              pairs: e.target.value === "where" && grant.pairs.length === 0 ? [{ column: "", attribute: "" }] : grant.pairs,
                            })
                          }
                        >
                          <option value="where">{t("Only rows matching")}</option>
                          <option value="all">{t("Every row")}</option>
                        </select>
                        {grant.mode === "where" && (
                          <WherePairs
                            testId={`write-where-${position}-${table}`}
                            pairs={grant.pairs}
                            columns={columnsOf(table)}
                            attributeNames={attributeNames}
                            columnLabel={t("Table column")}
                            onChange={(pairs) => setTableGrant(position, table, { ...grant, pairs })}
                          />
                        )}
                      </>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WherePairs({ testId, pairs, columns, attributeNames, onChange, columnLabel }) {
  const { t } = useLang();
  const setPair = (index, fields) => onChange(pairs.map((pair, position) => (position === index ? { ...pair, ...fields } : pair)));

  return (
    <div data-testid={testId} style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
      {pairs.map((pair, index) => (
        <div key={index} style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <select className="input" aria-label={columnLabel || t("View column")} value={pair.column} onChange={(e) => setPair(index, { column: e.target.value })}>
            <option value="">—</option>
            {columns.map((column) => <option key={column} value={column}>{column}</option>)}
          </select>
          <span>=</span>
          <select className="input" aria-label={t("Asker attribute")} value={pair.attribute} onChange={(e) => setPair(index, { attribute: e.target.value })}>
            <option value="">—</option>
            {attributeNames.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </div>
      ))}
      <button type="button" className="btn btn-ghost btn-sm" style={{ alignSelf: "flex-start" }} onClick={() => onChange([...pairs, { column: "", attribute: "" }])}>
        <Plus size={11} />{t("Add a condition")}
      </button>
    </div>
  );
}
