import { useState } from "react";
import { GitCompare, ArrowRight, Plus, Minus, AlertTriangle } from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Section, Spinner } from "../../components/ui";
import { useLang } from "../../i18n";

/* ============================================================
   What two versions of one agent differ by — #55 / ADR-0039.

   Two documents, compared separately and rendered separately: a `spec` is V1's
   pipeline, a `workflow` is V2's graph, and a version holds whichever ones its
   stage has reached. Merging them into one list would have to pretend a graph's
   nodes were a pipeline's steps, and they are not — a step is a position in a
   list, a node is a vertex with named output ports.

   Three states per document, and the screen has to tell them apart:
   `null` means neither version had it (nothing to say), `skipped` means exactly
   one of them did (a real finding, with a sentence), and a comparison object
   means both did.
   ============================================================ */
export default function VersionCompare({ agentId, versions }) {
  const { t } = useLang();
  const ordered = [...versions].sort((left, right) => right.id - left.id);
  const [beforeId, setBeforeId] = useState(ordered.length > 1 ? ordered[1].id : "");
  const [afterId, setAfterId] = useState(ordered.length > 0 ? ordered[0].id : "");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function compare() {
    if (!beforeId || !afterId || String(beforeId) === String(afterId)) {
      setError(t("Pick two different versions."));
      return;
    }
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(await api.agents.compare(agentId, beforeId, afterId));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (versions.length < 2) {
    return (
      <div className="ab-card ab-empty">
        {t("At least two versions are needed to compare. This agent has {n}.", { n: versions.length })}
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 14 }}>
        <div style={{ flex: "1 1 140px" }}>
          <label className="ab-label">{t("Before")}</label>
          <select className="ab-select" value={beforeId} onChange={(event) => setBeforeId(event.target.value)}>
            {ordered.map((version) => <option key={version.id} value={version.id}>#{version.id} · {version.status}</option>)}
          </select>
        </div>
        <ArrowRight size={15} style={{ color: "var(--ab-muted)", marginBottom: 11, flexShrink: 0 }} />
        <div style={{ flex: "1 1 140px" }}>
          <label className="ab-label">{t("After")}</label>
          <select className="ab-select" value={afterId} onChange={(event) => setAfterId(event.target.value)}>
            {ordered.map((version) => <option key={version.id} value={version.id}>#{version.id} · {version.status}</option>)}
          </select>
        </div>
        <button className="ab-btn ab-btn-outline ab-btn-sm" onClick={compare} disabled={busy} style={{ marginBottom: 1 }}>
          {busy ? <Spinner size={13} /> : <GitCompare size={13} />}So sánh
        </button>
      </div>

      <ErrorNote>{error}</ErrorNote>

      {result && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <p className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--ab-muted)", margin: 0 }}>
            #{result.version_a_id} → #{result.version_b_id}
          </p>

          {/* A document exactly one of the two versions had. Not "no changes" —
              that would tell a creator their versions are identical when one of
              them simply has not reached that stage. */}
          {Object.entries(result.skipped).map(([document, reason]) => (
            <div key={document} className="ab-card" style={{ padding: "var(--space-3)", borderColor: "rgba(245,158,11,.3)", background: "rgba(245,158,11,.05)" }}>
              <p style={{ display: "flex", gap: 8, fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
                <AlertTriangle size={14} style={{ color: "var(--ab-warn)", flexShrink: 0, marginTop: 2 }} />
                <span><b>{document}</b>: {reason}</span>
              </p>
            </div>
          ))}

          {result.workflow && <WorkflowDiff diff={result.workflow} />}
          {result.spec && <SpecDiff diff={result.spec} />}

          {!result.workflow && !result.spec && Object.keys(result.skipped).length === 0 && (
            <div className="ab-card ab-empty">{t("These two versions do not differ anywhere.")}</div>
          )}
        </div>
      )}
    </div>
  );
}


function WorkflowDiff({ diff }) {
  const { t } = useLang();
  const nodeChanges = Object.entries(diff.changed_nodes);
  const nothing =
    !diff.added_node_ids.length && !diff.removed_node_ids.length && !nodeChanges.length &&
    !diff.added_edges.length && !diff.removed_edges.length && !Object.keys(diff.changed_fields).length;

  return (
    <Section title="Workflow (graph)">
      {nothing && <p style={{ fontSize: "var(--text-sm)", color: "var(--ab-muted)", margin: 0 }}>{t("The graph is unchanged.")}</p>}

      <FieldChanges changes={diff.changed_fields} />

      <IdList label="Node thêm vào" ids={diff.added_node_ids} kind="added" />
      <IdList label="Node bỏ đi" ids={diff.removed_node_ids} kind="removed" />

      {nodeChanges.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <label className="ab-label">Node đổi cấu hình</label>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {nodeChanges.map(([nodeId, node]) => (
              <div key={nodeId} className="ab-version-item" style={{ alignItems: "flex-start" }}>
                <span className="ab-mono" style={{ fontSize: "var(--text-sm)", flexShrink: 0 }}>{nodeId}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  {node.changed_config_keys.length > 0 && (
                    <p style={{ fontSize: "var(--text-sm)", color: "var(--ab-muted)", margin: 0, lineHeight: 1.5 }}>
                      đổi: {node.changed_config_keys.map((key) => (
                        <span key={key} className="ab-mono" style={{ color: "var(--ab-fg)" }}>{key} </span>
                      ))}
                    </p>
                  )}
                  <FieldChanges changes={node.changed_fields} compact />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <EdgeList label="Nối thêm" edges={diff.added_edges} kind="added" />
      <EdgeList label="Bỏ nối" edges={diff.removed_edges} kind="removed" />

      <p style={{ fontSize: "var(--text-xs)", color: "var(--ab-muted)", margin: "12px 0 0", lineHeight: 1.5 }}>
        {t("A node's position on the canvas is not compared — dragging a node changes where it is drawn, not what it does. Edges are compared by")}{" "}
        <span className="ab-mono">{t("source · port · target")}</span>{" "}
        {t("rather than by id, so re-drawing an existing connection is not reported as a change.")}
      </p>
    </Section>
  );
}

function SpecDiff({ diff }) {
  const { t } = useLang();
  const stepChanges = Object.entries(diff.changed_steps);
  const nothing =
    !diff.added_step_ids.length && !diff.removed_step_ids.length && !stepChanges.length &&
    !Object.keys(diff.changed_fields).length;

  return (
    <Section title="Spec (pipeline)">
      {nothing && <p style={{ fontSize: "var(--text-sm)", color: "var(--ab-muted)", margin: 0 }}>{t("The pipeline is unchanged.")}</p>}
      <FieldChanges changes={diff.changed_fields} />
      <IdList label="Bước thêm vào" ids={diff.added_step_ids} kind="added" />
      <IdList label="Bước bỏ đi" ids={diff.removed_step_ids} kind="removed" />
      {stepChanges.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <label className="ab-label">Bước đổi nội dung</label>
          {stepChanges.map(([stepId, fields]) => (
            <div key={stepId} style={{ marginBottom: 10 }}>
              <span className="ab-mono" style={{ fontSize: "var(--text-sm)" }}>{stepId}</span>
              <FieldChanges changes={fields} compact />
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

/** `[before, after]` pairs, shown as before → after. Both halves are rendered
 *  because a diff that showed only the new value is a diff nobody can read
 *  backwards — and rollback is exactly reading it backwards. */
function FieldChanges({ changes, compact }) {
  const entries = Object.entries(changes || {});
  if (entries.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: compact ? 6 : 0 }}>
      {entries.map(([field, pair]) => (
        <div key={field}>
          <span className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--ab-muted)" }}>{field}</span>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 3, flexWrap: "wrap" }}>
            <Value value={pair[0]} tone="removed" />
            <ArrowRight size={12} style={{ color: "var(--ab-muted)", marginTop: 7, flexShrink: 0 }} />
            <Value value={pair[1]} tone="added" />
          </div>
        </div>
      ))}
    </div>
  );
}

function Value({ value, tone }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 1);
  const colour = tone === "removed" ? "rgba(239,68,68,.35)" : "color-mix(in srgb, var(--color-accent) 45%, transparent)";
  return (
    <pre
      className="ab-mono"
      style={{
        flex: "1 1 180px", minWidth: 0, margin: 0, fontSize: "var(--text-xs)", lineHeight: 1.5,
        background: "var(--color-surface)", border: `1px solid ${colour}`, borderRadius: 0,
        padding: "var(--space-1) var(--space-2)", whiteSpace: "pre-wrap", overflowX: "auto", maxHeight: 160, overflowY: "auto",
      }}
    >
      {text === undefined ? "—" : text}
    </pre>
  );
}

function IdList({ label, ids, kind }) {
  if (!ids || ids.length === 0) return null;
  const added = kind === "added";
  return (
    <div style={{ marginTop: 12 }}>
      <label className="ab-label">{label}</label>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {ids.map((id) => (
          <span
            key={id}
            className="ab-chip ab-mono"
            style={{ color: added ? "var(--color-accent)" : "#b91c1c", borderColor: added ? "var(--color-accent)" : "rgba(239,68,68,.35)" }}
          >
            {added ? <Plus size={10} /> : <Minus size={10} />}{id}
          </span>
        ))}
      </div>
    </div>
  );
}

function EdgeList({ label, edges, kind }) {
  if (!edges || edges.length === 0) return null;
  const added = kind === "added";
  return (
    <div style={{ marginTop: 12 }}>
      <label className="ab-label">{label}</label>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {edges.map((edge, index) => (
          <span
            key={index}
            className="ab-mono"
            style={{ fontSize: "var(--text-xs)", color: added ? "var(--ab-primary)" : "#f87171" }}
          >
            {added ? "+" : "−"} {edge.source_node_id} · {edge.source_port} → {edge.target_node_id}
          </span>
        ))}
      </div>
    </div>
  );
}
