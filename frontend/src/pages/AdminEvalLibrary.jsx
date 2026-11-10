import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, Pencil, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Note, PageHead, PageShell, Spinner } from "../components/ui";
import { AgentMascot } from "../components/mascot";
import ConfirmDialog from "../components/ConfirmDialog";
import { useLang } from "../i18n";

/* ============================================================
   ADR-0093 — the shared eval template library, on screen.

   "Dựng bộ đề" fills an agent's suite from `eval/library/*.jsonl`; until this
   screen, the only way to see what those templates said was to open the files.
   An admin reads every template here, narrows by tag, and adds, edits or
   removes one. Each edit rewrites the one file it sits in, so the change is a
   `git diff` like any other, and the server refuses a template that would not
   grade before anything is written.

   What an edit does NOT do is change a case already in a suite: a filled case
   holds its own text (ADR-0089). The card says how many live cases came from
   a template, so the admin knows how much of what is out there predates the
   edit.
   ============================================================ */

const muted = "color-mix(in srgb, var(--color-text) 62%, transparent)";

const EMPTY = {
  id: "",
  tags: [],
  applies_when: [],
  input: "",
  criterion: "",
  expected_check: '{\n  "kind": "takes_no_action"\n}',
  category: "",
  run_as: "",
};

/** The form's shape from a template the server sent. */
function formOf(entry) {
  return {
    id: entry.id,
    tags: [...entry.tags],
    applies_when: [...entry.applies_when],
    input: entry.input,
    criterion: entry.criterion,
    expected_check: JSON.stringify(entry.expected_check, null, 2),
    category: entry.category || "",
    run_as: entry.run_as || "",
  };
}

/** The template the server is sent from the form, or a reason it cannot be. */
function templateOf(form, t) {
  let check;
  try {
    check = JSON.parse(form.expected_check);
  } catch {
    return { error: t("The grading rule is not valid JSON.") };
  }
  const template = {
    id: form.id.trim(),
    tags: form.tags,
    applies_when: form.applies_when,
    input: form.input,
    criterion: form.criterion,
    expected_check: check,
  };
  if (form.category) template.category = form.category;
  if (form.run_as) template.run_as = form.run_as;
  return { template };
}

/** `{slot}`s drawn as chips inside the sentence, so a reader sees what code fills in. */
function Slotted({ text }) {
  const parts = text.split(/(\{[a-z_]+\})/g);
  return parts.map((part, index) =>
    /^\{[a-z_]+\}$/.test(part)
      ? <code key={index} className="ab-mono" style={{ fontSize: "0.92em", padding: "0 3px", background: "color-mix(in srgb, var(--color-accent) 14%, transparent)" }}>{part}</code>
      : <span key={index}>{part}</span>
  );
}

export default function AdminEvalLibrary({ notify }) {
  const { t } = useLang();
  const [library, setLibrary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tag, setTag] = useState("");
  const [query, setQuery] = useState("");
  // `{ mode: "new" | "edit", form, file }` while the editor is open.
  const [editing, setEditing] = useState(null);
  const [doomed, setDoomed] = useState(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setLibrary(await api.evalLibrary.list());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const templates = library?.templates || [];
  const tagCounts = useMemo(() => {
    const tally = {};
    for (const entry of templates) for (const name of entry.tags) tally[name] = (tally[name] || 0) + 1;
    return tally;
  }, [templates]);

  const needle = query.trim().toLowerCase();
  const shown = templates.filter((entry) => {
    if (tag && !entry.tags.includes(tag)) return false;
    if (!needle) return true;
    return [entry.id, entry.input, entry.criterion, entry.file].some((text) => text.toLowerCase().includes(needle));
  });

  async function confirmDelete() {
    setDeleting(true);
    try {
      await api.evalLibrary.remove(doomed.id);
      notify(t("Template {id} deleted", { id: doomed.id }));
      setDoomed(null);
      await load();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <PageShell>
      <PageHead
        title={t("Eval template library")}
        hint={t("The shared questions \"Build the suite\" fills every agent's test suite from. Each template applies only to agents that have the capabilities it names; the slots in braces are filled from the agent's own graph and data.")}
      >
        <button type="button" className="btn btn-secondary btn-icon" onClick={load} title={t("Reload")}>
          <RefreshCw size={16} />
        </button>
        <button type="button" className="btn btn-primary" onClick={() => setEditing({ mode: "new", form: { ...EMPTY }, file: "" })} disabled={!library}>
          <Plus size={14} />{t("New template")}
        </button>
      </PageHead>

      <Note text={t("Editing a template changes no case already in a suite — those keep the text they were filled with. A new template reaches an agent's suite the next time it is built. Every edit is written to eval/library/*.jsonl, so commit it like any other change.")} />
      <ErrorNote>{error}</ErrorNote>
      {library?.problems?.length > 0 && (
        <Note tone="warn" text={t("{n} line(s) in the library are not valid templates and are skipped: {list}", {
          n: library.problems.length,
          list: library.problems.map((problem) => `${problem.where} — ${problem.reason}`).join("; "),
        })} />
      )}

      {loading && !library && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}

      {library && (
        <>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", margin: "14px 0 12px" }}>
            <div style={{ position: "relative", flex: "1 1 240px", maxWidth: 360 }}>
              <Search size={13} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: muted }} />
              <input
                className="input"
                style={{ paddingLeft: 30, width: "100%" }}
                value={query}
                placeholder={t("Search id, question, criterion…")}
                aria-label={t("Search templates")}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <span className="text-muted" style={{ fontSize: "var(--text-sm)" }}>
              {t("{shown}/{total} templates", { shown: shown.length, total: templates.length })}
            </span>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 14 }}>
            <button type="button" className={`btn btn-sm ${tag === "" ? "btn-primary" : "btn-ghost"}`} onClick={() => setTag("")}>
              {t("All")} {templates.length}
            </button>
            {library.tags.filter((name) => tagCounts[name]).map((name) => (
              <button
                key={name}
                type="button"
                className={`btn btn-sm ${tag === name ? "btn-primary" : "btn-ghost"}`}
                onClick={() => setTag(tag === name ? "" : name)}
              >
                #{name} {tagCounts[name]}
              </button>
            ))}
          </div>

          {shown.length === 0 && (
            <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>{t("No template matches this filter.")}</div>
          )}

          <div className="ab-stack">
            {shown.map((entry) => (
              <TemplateCard
                key={entry.id}
                entry={entry}
                onEdit={() => setEditing({ mode: "edit", form: formOf(entry), file: entry.file })}
                onDelete={() => setDoomed(entry)}
              />
            ))}
          </div>
        </>
      )}

      {editing && library && (
        <TemplateEditor
          library={library}
          editing={editing}
          onClose={() => setEditing(null)}
          onSaved={async (saved, mode) => {
            setEditing(null);
            notify(mode === "new" ? t("Template {id} added to {file}", { id: saved.id, file: saved.file }) : t("Template {id} saved", { id: saved.id }));
            await load();
          }}
        />
      )}

      <ConfirmDialog
        open={Boolean(doomed)}
        busy={deleting}
        title={t("Delete this template?")}
        confirmLabel={t("Delete template")}
        onCancel={() => setDoomed(null)}
        onConfirm={confirmDelete}
        body={doomed && (
          <>
            <p className="ab-mono" style={{ margin: "0 0 8px" }}>{doomed.id}</p>
            <p style={{ margin: 0 }}>
              {t("It is removed from {file}. The {n} case(s) already filled from it stay in their suites.", { file: doomed.file, n: doomed.used_by_cases })}
            </p>
          </>
        )}
      />
    </PageShell>
  );
}

function TemplateCard({ entry, onEdit, onDelete }) {
  const { t } = useLang();
  return (
    <div className="card" style={{ gap: 8 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p className="ab-mono" style={{ margin: 0, fontSize: "var(--text-xs)", color: muted }}>
            {entry.id} · {entry.file}
          </p>
          <p style={{ margin: "4px 0 0", fontSize: "var(--text-sm)", lineHeight: 1.55 }}>
            <Slotted text={entry.input} />
          </p>
        </div>
        <button type="button" className="btn btn-secondary btn-icon" style={{ width: 28, height: 28, padding: "var(--space-1)", flexShrink: 0 }} onClick={onEdit} title={t("Edit this template")} aria-label={t("Edit this template")}>
          <Pencil size={13} />
        </button>
        <button type="button" className="btn btn-secondary btn-icon" style={{ width: 28, height: 28, padding: "var(--space-1)", flexShrink: 0 }} onClick={onDelete} title={t("Delete this template")} aria-label={t("Delete this template")}>
          <Trash2 size={13} />
        </button>
      </div>
      <p style={{ margin: 0, fontSize: "var(--text-sm)", color: muted }}>
        <b>{t("Criterion:")}</b> <Slotted text={entry.criterion} />
      </p>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {entry.tags.map((name) => <span key={name} className="ab-chip">#{name}</span>)}
        {entry.applies_when.map((name) => <span key={name} className="ab-chip" title={t("The agent must have this capability")}>{t("needs {capability}", { capability: name })}</span>)}
        {entry.run_as && <span className="ab-chip">{t("asked as {who}", { who: entry.run_as })}</span>}
        {entry.category && <span className="ab-chip">{entry.category}</span>}
        <span className="ab-chip" title={t("Live cases in agents' suites filled from this template")}>
          <BookOpen size={11} />{t("{n} case(s) in suites", { n: entry.used_by_cases })}
        </span>
      </div>
      <details>
        <summary style={{ fontSize: "var(--text-xs)", cursor: "pointer", color: muted }}>
          {t("grading rule: {kind}", { kind: entry.expected_check.kind })}
        </summary>
        <pre className="ab-mono" style={{ margin: "6px 0 0", fontSize: "var(--text-xs)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
          {JSON.stringify(entry.expected_check, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function TemplateEditor({ library, editing, onClose, onSaved }) {
  const { t } = useLang();
  const [form, setForm] = useState(editing.form);
  const [file, setFile] = useState(editing.file);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const isNew = editing.mode === "new";

  useEffect(() => {
    const onKey = (event) => event.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const edit = (patch) => setForm((current) => ({ ...current, ...patch }));
  const toggle = (key, value) =>
    setForm((current) => ({
      ...current,
      [key]: current[key].includes(value) ? current[key].filter((entry) => entry !== value) : [...current[key], value],
    }));

  async function save(event) {
    event.preventDefault();
    const { template, error: problem } = templateOf(form, t);
    if (problem) { setError(problem); return; }
    setBusy(true);
    setError("");
    try {
      const saved = isNew ? await api.evalLibrary.add(template, file) : await api.evalLibrary.replace(editing.form.id, template);
      await onSaved(saved, editing.mode);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const slotHint = Object.entries(library.slots)
    .map(([name, capability]) => (capability ? `{${name}} ← ${capability}` : `{${name}}`))
    .join(" · ");

  return (
    <div role="dialog" aria-modal="true" aria-label={isNew ? t("New template") : t("Edit template")} style={{ position: "fixed", inset: 0, zIndex: 60, display: "grid", placeItems: "center", background: "color-mix(in srgb, #000 45%, transparent)" }} onClick={() => !busy && onClose()}>
      <form
        onSubmit={save}
        onClick={(e) => e.stopPropagation()}
        style={{ width: "min(760px, 94vw)", maxHeight: "88vh", overflowY: "auto", background: "var(--color-bg)", border: "1px solid var(--color-divider)", padding: "var(--space-5)", display: "flex", flexDirection: "column", gap: 12 }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{isNew ? t("New template") : t("Edit template")}</p>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} disabled={busy} aria-label={t("Close")}><X size={16} /></button>
        </div>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "2 1 260px" }}>
            <label htmlFor="tpl-id">{t("Id")}</label>
            <input id="tpl-id" className="input ab-mono" value={form.id} disabled={!isNew} placeholder="injection-my-case-01" onChange={(e) => edit({ id: e.target.value })} />
            {!isNew && <span className="text-muted" style={{ fontSize: "var(--text-xs)" }}>{t("An id cannot change — cases already built remember it.")}</span>}
          </div>
          <div className="field" style={{ flex: "1 1 180px" }}>
            <label htmlFor="tpl-file">{t("File")}</label>
            <select id="tpl-file" className="input" value={file} disabled={!isNew} onChange={(e) => setFile(e.target.value)}>
              {isNew && <option value="">{t("— by the first tag —")}</option>}
              {library.files.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </div>
        </div>

        <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
          <label>{t("Tags")}</label>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: "var(--text-sm)" }}>
            {library.tags.map((name) => (
              <label key={name} style={{ display: "inline-flex", gap: 5, alignItems: "center" }}>
                <input type="checkbox" checked={form.tags.includes(name)} onChange={() => toggle("tags", name)} />{name}
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
          <label>{t("Applies when the agent has")}</label>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: "var(--text-sm)" }}>
            {library.capabilities.map((name) => (
              <label key={name} style={{ display: "inline-flex", gap: 5, alignItems: "center" }}>
                <input type="checkbox" checked={form.applies_when.includes(name)} onChange={() => toggle("applies_when", name)} />{name}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="field">
          <label htmlFor="tpl-input">{t("Question asked to the agent")}</label>
          <textarea id="tpl-input" className="input" rows={3} value={form.input} onChange={(e) => edit({ input: e.target.value })} />
          <span className="text-muted ab-mono" style={{ fontSize: "var(--text-2xs)", lineHeight: 1.5 }}>{t("Slots:")} {slotHint}</span>
        </div>

        <div className="field">
          <label htmlFor="tpl-criterion">{t("Criterion")}</label>
          <input id="tpl-criterion" className="input" value={form.criterion} onChange={(e) => edit({ criterion: e.target.value })} />
        </div>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "1 1 200px" }}>
            <label htmlFor="tpl-category">{t("Category")}</label>
            <select id="tpl-category" className="input" value={form.category} onChange={(e) => edit({ category: e.target.value })}>
              <option value="">{t("— none —")}</option>
              {library.categories.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </div>
          <div className="field" style={{ flex: "1 1 200px" }}>
            <label htmlFor="tpl-runas">{t("Asked as")}</label>
            <select id="tpl-runas" className="input" value={form.run_as} onChange={(e) => edit({ run_as: e.target.value })}>
              <option value="">{t("— default —")}</option>
              {library.run_as.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </div>
        </div>

        <div className="field">
          <label htmlFor="tpl-check">{t("Grading rule (JSON)")}</label>
          <textarea id="tpl-check" className="input ab-mono" rows={7} value={form.expected_check} onChange={(e) => edit({ expected_check: e.target.value })} style={{ fontSize: "var(--text-xs)" }} />
          <span className="text-muted" style={{ fontSize: "var(--text-xs)" }}>
            {t("takes_no_action · asks_back · forbids_action · approval_compliance · answers_from_query + expects_no_rows · cites_knowledge — the server checks the rule grades once filled.")}
          </span>
        </div>

        <ErrorNote>{error}</ErrorNote>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>{t("Cancel")}</button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? <Spinner size={13} /> : null}{isNew ? t("Add template") : t("Save template")}
          </button>
        </div>
      </form>
    </div>
  );
}
