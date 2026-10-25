import { useEffect, useState } from "react";
import { Check, FileCode, Plus, RefreshCw, Trash2 } from "lucide-react";
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
   ============================================================ */

const POSITION = "position";

const GRANTS = [
  { id: "deny", label: "Deny" },
  { id: "all", label: "Everything" },
  { id: "where", label: "Filter" },
];

const emptyDraft = (source) => ({
  view: source.allowed_views[0] || "",
  matchBy: "",
  attributes: [{ name: POSITION, column: "" }],
  positions: [],
  grants: {},
});

/** The saved model → the editable draft. */
function draftOf(rules) {
  const grants = {};
  for (const rule of rules.rules) {
    grants[rule.position] = Object.fromEntries(
      Object.entries(rule.views).map(([view, grant]) => [
        view,
        grant === "all"
          ? { mode: "all", pairs: [] }
          : { mode: "where", pairs: Object.entries(grant.where).map(([column, attribute]) => ({ column, attribute })) },
      ]),
    );
  }
  return {
    view: rules.identity.view,
    matchBy: rules.identity.match_by,
    attributes: Object.entries(rules.identity.attributes).map(([name, column]) => ({ name, column })),
    positions: rules.rules.map((rule) => rule.position),
    grants,
  };
}

/** The draft → the `access.yaml` model. Deny cells and incomplete filter pairs are simply not written. */
function toRules(draft, grantViews) {
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
      return { position, views };
    })
    .filter((rule) => Object.keys(rule.views).length > 0);
  return { identity: { view: draft.view, match_by: draft.matchBy, attributes }, rules };
}

/** Every `[position, view]` set to Filter with no complete pair. `toRules` would drop it — a deny nobody chose. */
function incompleteFilters(draft, grantViews) {
  return draft.positions.flatMap((position) =>
    grantViews
      .filter((view) => {
        const grant = draft.grants[position]?.[view];
        return grant?.mode === "where" && !grant.pairs.some((pair) => pair.column && pair.attribute);
      })
      .map((view) => [position, view]),
  );
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

  useEffect(() => {
    let cancelled = false;
    api.databaseSources
      .accessRules(source.id)
      .then((saved) => {
        if (cancelled) return;
        if (saved.rules) setDraft(draftOf(saved.rules));
        setProblems(saved.problems || []);
      })
      .catch((err) => notify(err.message, "error"))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [source.id, notify]);

  const columnsOf = (view) => source.views.find((entry) => entry.name === view)?.columns || [];
  const grantViews = source.allowed_views.filter((view) => view !== draft.view);
  const attributeNames = draft.attributes.map((row) => row.name.trim()).filter(Boolean);
  const positionColumn = draft.attributes.find((row) => row.name === POSITION)?.column;
  const edit = (fields) => setDraft((current) => ({ ...current, ...fields }));

  function setGrant(position, view, grant) {
    setDraft((current) => ({
      ...current,
      grants: { ...current.grants, [position]: { ...current.grants[position], [view]: grant } },
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

  async function save() {
    const incomplete = incompleteFilters(draft, grantViews).map(([position, view]) =>
      t("{position} — {view}: the filter has no complete condition. Pick a view column and an asker attribute, or choose Deny.", { position, view }),
    );
    if (incomplete.length) {
      setProblems(incomplete);
      return;
    }
    setSaving(true);
    try {
      const saved = await api.databaseSources.saveAccessRules(source.id, toRules(draft, grantViews));
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

  async function showYaml() {
    try {
      setYamlText(await api.databaseSources.accessRulesYaml(source.id));
    } catch (err) {
      notify(err.status === 404 ? t("Save the rules first to see them as YAML.") : err.message, "error");
    }
  }

  if (loading) return <Spinner size={14} />;

  const nothingGranted = toRules(draft, grantViews).rules.length === 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <IdentityFields draft={draft} source={source} columnsOf={columnsOf} edit={edit} />

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

function WherePairs({ testId, pairs, columns, attributeNames, onChange }) {
  const { t } = useLang();
  const setPair = (index, fields) => onChange(pairs.map((pair, position) => (position === index ? { ...pair, ...fields } : pair)));

  return (
    <div data-testid={testId} style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
      {pairs.map((pair, index) => (
        <div key={index} style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <select className="input" aria-label={t("View column")} value={pair.column} onChange={(e) => setPair(index, { column: e.target.value })}>
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
