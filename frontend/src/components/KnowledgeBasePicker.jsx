import { useCallback, useEffect, useState } from "react";
import { Library, Database, RefreshCw, Plus } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Spinner } from "./ui";

/* ============================================================
   Pick Knowledge Bases for a `KnowledgeSearch` node's `source_ids`.

   A shelf is ticked from a list, never typed — RAG-05 (#91) made `source_ids`
   hold Knowledge Base ids, and RAG-13 (#99) is what replaces the old per-file
   picker with this one. A `KnowledgeSearch` node addresses a shelf, so this is
   also where a creator who has not built one yet can start one on the spot.
   ============================================================ */
export default function KnowledgeBasePicker({ selected, onChange, disabled }) {
  const [shelves, setShelves] = useState([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setShelves(await api.knowledgeBases.list());
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const chosen = new Set((selected || []).map(String));

  const toggle = (kbId) => {
    if (disabled) return;
    const id = String(kbId);
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id); else next.add(id);
    onChange([...next]);
  };

  async function createShelf(event) {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    setError("");
    try {
      const kb = await api.knowledgeBases.create(name);
      setNewName("");
      await load();
      onChange([...chosen, String(kb.id)]);
    } catch (err) {
      setError(err.message);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <label className="ab-label" style={{ margin: 0 }}>Knowledge Bases searched</label>
        <button className="ab-btn ab-btn-ghost ab-btn-sm" style={{ padding: "var(--space-1)" }} onClick={load} title="Reload">
          <RefreshCw size={12} />
        </button>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
        {loading && <div style={{ padding: "var(--space-2) 0" }}><Spinner size={14} /></div>}

        {!loading && shelves.length === 0 && (
          <p style={{ fontSize: "var(--text-2xs)", color: "var(--ab-muted)", margin: 0, lineHeight: 1.5 }}>
            No Knowledge Base yet — create one below, then add documents to it later.
          </p>
        )}

        {shelves.map((kb) => {
          const id = String(kb.id);
          const on = chosen.has(id);
          return (
            <div
              key={id}
              className="ab-card"
              style={{
                display: "flex", alignItems: "center", gap: 8, padding: "var(--space-2)",
                borderColor: on ? "rgba(0,212,168,.45)" : "var(--ab-border)",
                background: on ? "rgba(0,212,168,.05)" : "rgba(255,255,255,.015)",
              }}
            >
              <input
                type="checkbox"
                checked={on}
                disabled={disabled}
                onChange={() => toggle(id)}
                style={{ accentColor: "var(--ab-primary)", flexShrink: 0 }}
              />
              {/* Which kind of shelf this is, at the point where a node is
                  pointed at it (RAG-22 / #108) — a BYODB shelf searches
                  dense-only, so "which one did I attach" is worth answering
                  here and not only on the Knowledge Bases screen. */}
              {kb.mode === "byodb"
                ? <Database size={13} style={{ color: on ? "var(--ab-sky)" : "var(--ab-muted)", flexShrink: 0 }} />
                : <Library size={13} style={{ color: on ? "var(--ab-primary)" : "var(--ab-muted)", flexShrink: 0 }} />}
              <button
                onClick={() => toggle(id)}
                disabled={disabled}
                style={{ flex: 1, minWidth: 0, background: "none", border: "none", color: "var(--ab-fg)", fontSize: "var(--text-xs)", textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                title={kb.name}
              >
                {kb.name}
              </button>
              <span className="ab-mono" style={{ fontSize: "var(--text-2xs)", color: "var(--ab-muted)", flexShrink: 0 }}>
                {kb.mode === "byodb" ? "kết nối ngoài" : `${kb.document_count} tài liệu`}
              </span>
            </div>
          );
        })}

        {/* A shelf the node still names that no longer exists — deleted since
            this graph was drawn — has to be shown, not silently dropped from
            the config about to be saved. */}
        {(selected || []).filter((id) => !shelves.some((kb) => String(kb.id) === String(id))).map((id) => (
          <div key={id} className="ab-card" style={{ display: "flex", alignItems: "center", gap: 8, padding: "var(--space-2)", borderColor: "rgba(239,68,68,.3)", background: "rgba(239,68,68,.05)" }}>
            <Library size={13} style={{ color: "#f87171", flexShrink: 0 }} />
            <span className="ab-mono" style={{ flex: 1, minWidth: 0, fontSize: "var(--text-2xs)", color: "#f8a5a5" }}>Knowledge Base #{id}</span>
            <span style={{ fontSize: "var(--text-2xs)", color: "#f8a5a5", flexShrink: 0 }}>missing</span>
            {!disabled && (
              <button className="ab-btn ab-btn-ghost ab-btn-icon" style={{ padding: 0 }} onClick={() => toggle(id)} title="Remove from this step">
                ×
              </button>
            )}
          </div>
        ))}
      </div>

      {!disabled && (
        <form onSubmit={createShelf} style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <input
            className="ab-input"
            style={{ flex: 1, fontSize: "var(--text-xs)" }}
            placeholder="New Knowledge Base name…"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
          <button className="ab-btn ab-btn-outline ab-btn-sm" type="submit" disabled={creating || !newName.trim()}>
            {creating ? <Spinner size={12} /> : <Plus size={12} />}
          </button>
        </form>
      )}

      {error && <div style={{ marginTop: 8 }}><ErrorNote>{error}</ErrorNote></div>}

      <p style={{ fontSize: "var(--text-2xs)", color: "var(--ab-muted)", margin: "8px 0 0", lineHeight: 1.5 }}>
        A Knowledge Base is a shelf you add documents to from the Knowledge Bases screen — adding a
        document to it later is not a workflow change.
      </p>
    </div>
  );
}
