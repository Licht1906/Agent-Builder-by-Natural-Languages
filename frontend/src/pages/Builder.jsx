import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Layers, Play, ListChecks, Rocket, Activity, Save, CheckCircle2, AlertCircle, Plus, GitBranch, Settings2, X } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Spinner, StatusBadge } from "../components/ui";
import { AgentMascot } from "../components/mascot";
import { useAgentActivity, useAgentPose, useAgentSide, useAgentStandIn } from "../components/agent-presence";
import { PALETTE_ORDER, lookOf, blankConfig, freshNodeId, freshEdgeId, spreadStackedPositions } from "../lib/nodes";
import BuilderCanvas from "./builder/Canvas";
import GraphChat from "./builder/GraphChat";
import Inspector from "./builder/Inspector";
import { usePaneSize, PaneResizer } from "../lib/panes";
import SandboxTab from "./builder/SandboxTab";
import EvaluationTab from "./builder/EvaluationTab";
import DeployTab from "./builder/DeployTab";
import OperationsTab from "./builder/OperationsTab";
import { useLang } from "../i18n";

/* Operations is its own tab rather than a panel on Approval & Deploy — that one
   answers "may this version run", this one answers "how is the version that is
   running behaving" (ADR-0039). They are read at different times. */
const TABS = [
  { id: "canvas", label: "Canvas", icon: Layers },
  { id: "sandbox", label: "Sandbox", icon: Play },
  { id: "evaluation", label: "Evaluation", icon: ListChecks },
  { id: "deploy", label: "Approval & Deploy", icon: Rocket },
  { id: "operations", label: "Operations", icon: Activity },
];

/** The one stage whose graph is edited **in place**. */
const EDITABLE = new Set(["workflow_pending_confirmation"]);

/** The stages whose graph is frozen but where an edit still has somewhere to go:
 *  the backend forks it onto a new version (`WORKFLOW_FORKABLE`).
 *
 *  Kept behind a click rather than simply unlocking the canvas. A fork is a real
 *  event — it mints a version, and on an approved one it is the first step of
 *  changing what people are talking to — and discovering you caused it by
 *  dragging a node is how somebody ends up with four versions they did not mean
 *  to make. So the canvas stays read-only until the creator says the words. */
const FORKABLE = new Set([
  "workflow_confirmed",
  "sandbox_passed",
  "pending_approval",
  "approved",
  "rejected",
]);

/** Why this version is frozen, said the way the creator would say it. */
const FROZEN_BECAUSE = {
  workflow_confirmed: "This workflow is confirmed, so its graph is frozen.",
  sandbox_passed: "This version has a sandbox run behind it, so its graph is frozen.",
  pending_approval: "This version is waiting on an admin, so its graph is frozen.",
  approved: "This version is approved — it is what your published agent runs.",
  rejected: "An admin sent this version back, so its graph is frozen.",
};

/** One shared empty set rather than a fresh `new Set()` each render — the canvas
 *  memoises on this prop, and a new object every time would defeat that. */
const EMPTY_IDS = new Set();

/** How long a node the chat just touched stays lit. Long enough to find the box
 *  after reading the sentence that named it, short enough that it is gone before
 *  the next edit, so two turns never leave the canvas ringed in stale marks. */
const HIGHLIGHT_MS = 2500;

export default function BuilderPage({ agent, versionId, setVersionId, notify }) {
  const { t } = useLang();
  const [name, setName] = useState(agent.name);
  const [draft, setDraft] = useState(null);
  const [nodeTypes, setNodeTypes] = useState({});
  const [tools, setTools] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [activeNodeId, setActiveNodeId] = useState(null);
  const [changedNodeIds, setChangedNodeIds] = useState(EMPTY_IDS);
  const [tab, setTab] = useState("canvas");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState("");

  /* The version's stage when it has no graph at all — `null` whenever it has
     one. Kept apart from `error` because it is not one: a confirmed spec with no
     workflow yet is a step in the ordinary path, and the canvas has something to
     offer for it. */
  const [ungenerated, setUngenerated] = useState(null);

  /* Set by the "Edit as a new version" button and cleared whenever a different
     version is loaded. Not derived from the status, because the whole point is
     that it is a decision the creator made about *this* opening of the canvas —
     a derived flag would silently unlock the next version they navigate to. */
  const [forking, setForking] = useState(false);

  /* The issues list lives behind the red chip in the header rather than on a
     strip under the canvas: the strip repeated what the red-ringed nodes
     already say, and it ate canvas height whether or not anyone was reading
     it. The chip's count is the summons; the popover is the detail. */
  const [showIssues, setShowIssues] = useState(false);

  /* The agent-wide settings (disclaimer) used to be what the inspector showed
     when nothing was selected — which meant a permanent right-hand column just
     in case. Now the inspector floats only when called for: a selected node,
     or this gear. */
  const [showSettings, setShowSettings] = useState(false);

  /* The palette starts closed: the graph is what the screen is for, and a
     column of node types you are not adding right now is furniture. The button
     that summons it sits where the palette will appear. */
  const [showPalette, setShowPalette] = useState(false);

  /* Escape closes it, the reflex for anything floating over a canvas. Bound
     only while it is open, so nothing else on this screen has to know. */
  useEffect(() => {
    if (!showPalette) return undefined;
    const onKey = (event) => { if (event.key === "Escape") setShowPalette(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showPalette]);

  const [paletteW, resizePalette, resetPalette] = usePaneSize("builder.palette", 214, 150, 360);
  const [inspectorW, resizeInspector, resetInspector] = usePaneSize("builder.inspector", 280, 210, 480);

  /* The graph the server has actually seen. "Unsaved" is *derived* from it
     rather than kept as a flag, which is what makes the auto-save below safe:
     a flag can be cleared by a request that finished after an edit it knew
     nothing about, and the edit is then never saved. An identity comparison
     cannot get that wrong — either this is the object we sent, or it is not. */
  /* Every long operation on this screen, declared once. The shell's agent
     shows whichever is live; nothing here draws a mascot of its own except the
     empty state below, which is about this page's own middle. */
  /* The Canvas tab is where a workflow is drawn, so the agent wears the
     blueprint the whole time it is open — not only during the seconds a
     generation is actually running. A pose rather than an activity: declared
     as an activity, "builder" would rank above the thinking and exploring
     states of everything that runs on this tab and quietly mask them.

     The canvas also carries GraphChat, bottom-centre over the graph, so the
     agent steps to the other corner rather than standing opposite the bar the
     person is typing into. */
  useAgentPose("builder", tab === "canvas");
  useAgentSide("left", tab === "canvas");

  useAgentActivity("builder", busy === "generate", t("composing the workflow"));
  useAgentActivity("processing", busy === "save", t("saving the workflow"));
  useAgentActivity("processing", busy === "confirm", t("confirming the workflow"));
  useAgentActivity("exploring", loading, t("opening the workflow"));

  const syncedGraph = useRef(null);
  const dirty = Boolean(draft) && draft.workflow !== syncedGraph.current;

  const status = draft ? draft.status : null;
  const editsInPlace = Boolean(status) && EDITABLE.has(status);
  const canFork = Boolean(status) && FORKABLE.has(status);
  const readOnly = !draft || !(editsInPlace || (canFork && forking));

  /* The palette and every property form come from the server's registry, so a
     node type this app has never heard of still gets a working editor. */
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.workflow.nodeTypes(), api.workflow.tools().catch(() => [])])
      .then(([types, catalog]) => {
        if (cancelled) return;
        setNodeTypes(Object.fromEntries(types.map((entry) => [entry.type, entry])));
        setTools(catalog);
      })
      .catch((err) => !cancelled && setError(err.message));
    return () => { cancelled = true; };
  }, []);

  /* Re-read the connection catalogue after the Inspector registers one, so a
     server added from the node appears in that node's dropdown the moment an
     admin approves it — without the creator reloading the whole canvas and
     losing where they were. */
  const reloadTools = useCallback(() => {
    api.workflow.tools().then(setTools).catch(() => {});
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setUngenerated(null);
    // A different version is a different decision about whether to fork it.
    setForking(false);
    try {
      const loaded = await api.workflow.read(agent.id, versionId);
      // A seeded or imported graph can arrive with every node on one point;
      // spread it so the canvas shows the graph, not a pile (`lib/nodes.js`).
      // The spread graph goes into BOTH sides of the dirty comparison, so
      // reading never counts as an edit and nothing is saved on its own.
      const shown = { ...loaded, workflow: spreadStackedPositions(loaded.workflow) };
      // What was just read is by definition what the server has seen.
      syncedGraph.current = shown.workflow;
      setDraft(shown);
    } catch (err) {
      // "Version này chưa có workflow nào" is not a failure — it is the ordinary
      // state of a version whose spec is confirmed and whose graph has not been
      // drawn yet, and it is reachable from the sidebar without ever passing the
      // Generate button on the spec screen. Printing the sentence in red left a
      // creator on a blank canvas with the one thing that would fix it on
      // another page, so ask what stage this version is really at and offer it.
      try {
        const { status: stage } = await api.spec.read(agent.id, versionId);
        setUngenerated(stage);
      } catch {
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  }, [agent.id, versionId]);

  /* Compose a graph for this version's own spec, from the canvas that is missing
     one. The same call the spec screen's button makes — deliberately not a
     second endpoint and not a second set of rules about which stage may do it:
     a version at the wrong stage is refused by the API, in a sentence. */
  const generate = () => act("generate", async () => {
    const drafted = await api.workflow.generate(agent.id, versionId);
    syncedGraph.current = drafted.workflow;
    setUngenerated(null);
    setDraft(drafted);
    if (drafted.version_id !== versionId) setVersionId(drafted.version_id);
  }, t("Workflow composed for your specification"));

  useEffect(() => { load(); }, [load]);

  const problemNodeIds = useMemo(
    () => new Set((draft ? draft.problems : []).map((problem) => problem.node_id).filter(Boolean)),
    [draft]
  );

  const editGraph = (change) => setDraft((current) => ({ ...current, workflow: change(current.workflow) }));

  /* A whole graph swapped in — what a chat turn produces, and what undoing one
     puts back. Deliberately routed through `editGraph` like every drag and every
     palette click: `dirty` is an identity comparison against the last graph the
     server acknowledged, so an edit that bypassed this would either never be
     saved or be saved without the canvas knowing it had been. */
  const applyGraph = useCallback((next, touched) => {
    editGraph(() => next);
    setChangedNodeIds(touched && touched.size ? touched : EMPTY_IDS);
  }, []);

  /* The highlight is a hint, not state anything depends on, so it expires on its
     own rather than waiting for the next interaction to clear it. */
  useEffect(() => {
    if (changedNodeIds.size === 0) return undefined;
    const timer = setTimeout(() => setChangedNodeIds(EMPTY_IDS), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [changedNodeIds]);

  /* ------------------------------------------------------------------
     Re-validate by saving, shortly after the typing stops.

     `problems` is the backend's answer, not this app's — that is the whole
     reason the canvas draws the same list the confirm gate refuses on. Which
     means a config field filled in locally cannot clear its own warning: the
     warning is a fact about the last graph the server saw. Without this, a
     creator fixes a blank prompt and the red ring stays, which reads as "the
     fix did not work".

     Saving is the right way to ask. `PUT .../workflow` is built for exactly
     this — it never refuses a half-finished graph, it answers with the problems
     so the canvas can draw them. Debounced so a drag across the canvas is one
     request at the end rather than one per frame.
     ------------------------------------------------------------------ */
  useEffect(() => {
    if (!dirty || readOnly || !draft) return undefined;
    const pending = draft.workflow;
    const timer = setTimeout(async () => {
      setSyncing(true);
      try {
        const saved = await api.workflow.save(agent.id, versionId, pending);
        if (saved.version_id !== versionId) {
          // The edit forked. Said out loud, because from here on the creator is
          // editing a different version than the one they opened — and the
          // published one, if there was one, is still serving the old graph.
          notify(t("Your edits landed on a new version #{n} — the published version is untouched", { n: saved.version_number }));
          setVersionId(saved.version_id);
        }
        setDraft((current) => {
          if (current.workflow === pending) {
            // Nothing changed while this was in flight: take the response whole,
            // and it becomes what the server has seen.
            syncedGraph.current = saved.workflow;
            return saved;
          }
          // More edits landed. Keep them, take only the verdict — and record
          // that `pending` is synced, which leaves the newer graph dirty so the
          // next cycle re-asks about it.
          syncedGraph.current = pending;
          return { ...current, status: saved.status, problems: saved.problems, warnings: saved.warnings };
        });
        setError("");
      } catch (err) {
        setError(err.message);
      } finally {
        setSyncing(false);
      }
    }, 600);
    return () => clearTimeout(timer);
  }, [dirty, draft, readOnly, agent.id, versionId, setVersionId, notify]);

  /* ---- graph edits ---- */

  const moveNode = (nodeId, x, y) =>
    editGraph((workflow) => ({
      ...workflow,
      nodes: workflow.nodes.map((node) => (node.id === nodeId ? { ...node, position: { x, y } } : node)),
    }));

  const addNode = (type, position = null) => {
    const spec = nodeTypes[type];
    if (!spec) return;
    editGraph((workflow) => {
      const id = freshNodeId(type, workflow.nodes);
      const pos = position || { x: 80, y: 80 + workflow.nodes.length * 24 };
      const node = { id, type, config: blankConfig(spec.config_schema), position: pos };
      setSelectedId(id);
      return { ...workflow, nodes: [...workflow.nodes, node] };
    });
  };

  const deleteNode = (nodeId) => {
    editGraph((workflow) => ({
      ...workflow,
      nodes: workflow.nodes.filter((node) => node.id !== nodeId),
      edges: workflow.edges.filter((edge) => edge.source_node_id !== nodeId && edge.target_node_id !== nodeId),
    }));
    setSelectedId(null);
  };

  /** Renaming a node moves every edge and, if it was the entry point, the
   *  `start_node_id` with it — an id is what the whole document points at. */
  const renameNode = (nodeId, nextId) => {
    editGraph((workflow) => ({
      ...workflow,
      start_node_id: workflow.start_node_id === nodeId ? nextId : workflow.start_node_id,
      nodes: workflow.nodes.map((node) => (node.id === nodeId ? { ...node, id: nextId } : node)),
      edges: workflow.edges.map((edge) => ({
        ...edge,
        source_node_id: edge.source_node_id === nodeId ? nextId : edge.source_node_id,
        target_node_id: edge.target_node_id === nodeId ? nextId : edge.target_node_id,
      })),
    }));
    setSelectedId(nextId);
  };

  const setConfig = (nodeId, patch) => {
    editGraph((workflow) => {
      const nodes = workflow.nodes.map((node) => (node.id === nodeId ? { ...node, config: { ...node.config, ...patch } } : node));
      // Every Knowledge Search node's source_ids, unioned, is this agent's real
      // "which shelves does it use" — kept in sync with `agent_kb_links` so
      // RAG-13's shelf list can say so without parsing every version's graph.
      // Best-effort: a failure here does not block the canvas edit itself.
      if ("source_ids" in patch) {
        const kbIds = [...new Set(nodes.filter((node) => node.type === "knowledge_search").flatMap((node) => node.config.source_ids || []))].map(Number);
        api.agents.setKnowledgeBases(agent.id, kbIds).catch(() => {});
      }
      return { ...workflow, nodes };
    });
  };

  const setMetadata = (patch) => editGraph((workflow) => ({ ...workflow, metadata: { ...workflow.metadata, ...patch } }));

  /** At most one edge may leave a named output port, so wiring a port that
   *  already has one replaces it rather than adding a second. */
  const connect = (sourceNodeId, sourcePort, targetNodeId) =>
    editGraph((workflow) => {
      const kept = workflow.edges.filter(
        (edge) => !(edge.source_node_id === sourceNodeId && edge.source_port === sourcePort)
      );
      return {
        ...workflow,
        edges: [...kept, { id: freshEdgeId(workflow.edges), source_node_id: sourceNodeId, source_port: sourcePort, target_node_id: targetNodeId }],
      };
    });

  const deleteEdge = (edgeId) =>
    editGraph((workflow) => ({ ...workflow, edges: workflow.edges.filter((edge) => edge.id !== edgeId) }));

  const makeStart = (nodeId) => editGraph((workflow) => ({ ...workflow, start_node_id: nodeId }));

  /* ---- saves ---- */

  async function act(kind, action, success) {
    setBusy(kind);
    setError("");
    try {
      await action();
      if (success) notify(success);
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  /** Save right now rather than waiting out the debounce. Returns the version
   *  the graph actually landed on — editing a confirmed workflow forks, and the
   *  response is the only thing that knows where it went. */
  async function saveNow() {
    const pending = draft.workflow;
    const saved = await api.workflow.save(agent.id, versionId, pending);
    syncedGraph.current = saved.workflow;
    setDraft(saved);
    if (saved.version_id !== versionId) {
      notify(t("Your edits landed on a new version #{n} — the published version is untouched", { n: saved.version_number }));
      setVersionId(saved.version_id);
    }
    return saved.version_id;
  }

  const save = () => act("save", saveNow, t("Workflow saved"));

  const confirm = () => act("confirm", async () => {
    const target = dirty ? await saveNow() : versionId;
    const confirmed = await api.workflow.confirm(agent.id, target);
    syncedGraph.current = confirmed.workflow;
    setDraft(confirmed);
  }, t("Workflow confirmed — the graph is immutable from here"));

  const commitName = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === agent.name) return;
    try {
      await api.agents.rename(agent.id, trimmed);
      agent.name = trimmed;
    } catch (err) {
      setName(agent.name);
      notify(err.message, "error");
    }
  };

  const selectedNode = draft ? draft.workflow.nodes.find((node) => node.id === selectedId) || null : null;
  const selectedSchema = selectedNode && nodeTypes[selectedNode.type] ? nodeTypes[selectedNode.type].config_schema : null;

  /* Any click on the canvas — node or background — settles what the floating
     inspector shows, so it also closes the agent-settings view: the gear
     reopens it, and a background click reads as "give me the graph back". */
  const select = useCallback((id) => {
    setSelectedId(id);
    setShowSettings(false);
  }, []);

  const issueCount = draft ? draft.problems.length + draft.warnings.length : 0;

  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden", position: "relative", background: "var(--color-bg)", color: "var(--color-text)" }}>
      {/* One slim bar: identity, the tabs, the stage's actions. Everything else
          floats over the canvas, so this row is the only chrome allowed to cost
          the graph height — which is why the tabs moved in here instead of
          keeping a row of their own. */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "var(--space-1) var(--space-3)", borderBottom: "1px solid var(--color-divider)", flexWrap: "wrap", flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <input
            className="input"
            style={{ width: 170, fontWeight: 500, background: "transparent", border: "1px solid transparent", padding: "var(--space-1) var(--space-2)", minHeight: 30 }}
            value={name}
            title={t("Agent name — saved when you click away")}
            onChange={(event) => setName(event.target.value)}
            onBlur={commitName}
            onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
          />
          <StatusBadge status={draft && draft.status} />
          <span className="ab-mono" title={`Internal id ${versionId}`} style={{ fontSize: "var(--text-xs)", color: "var(--color-accent)" }}>v#{draft ? draft.version_number : "…"}</span>
          {issueCount > 0 && (
            <button
              className="tag"
              onClick={() => setShowIssues((shown) => !shown)}
              title={t("Open the issues list")}
              style={draft.problems.length > 0
                ? { cursor: "pointer", color: "#f87171", background: "rgba(239,68,68,.1)", border: "1px solid rgba(239,68,68,.25)" }
                : { cursor: "pointer", color: "var(--ab-warn)", background: "color-mix(in srgb, #f59e0b 12%, transparent)", border: "1px solid color-mix(in srgb, #f59e0b 35%, transparent)" }}
            >
              <AlertCircle size={11} />
              {draft.problems.length > 0 ? t("{n} to fix", { n: draft.problems.length }) : t("{n} warning(s)", { n: draft.warnings.length })}
            </button>
          )}
        </div>

        <div className="ab-tabs-inline" style={{ flex: "1 1 auto", justifyContent: "center", minWidth: 0 }}>
          {TABS.map((entry) => (
            <button key={entry.id} className={`ab-tab${tab === entry.id ? " active" : ""}`} onClick={() => setTab(entry.id)}>
              <entry.icon size={13} />{t(entry.label)}
            </button>
          ))}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
          {!readOnly && (
            <>
              <span className="tag tag-outline" style={{ minWidth: 84, justifyContent: "center" }}>
                {syncing ? <><Spinner size={11} />{t("Saving…")}</> : dirty ? t("Unsaved") : t("Saved")}
              </span>
              <button className="btn btn-secondary btn-sm" onClick={save} disabled={!!busy || !dirty}>
                {busy === "save" ? <Spinner size={13} /> : <Save size={13} />}{t("Save now")}
              </button>
              <button
                className="btn btn-primary btn-sm"
                onClick={confirm}
                disabled={!!busy || (draft && draft.problems.length > 0)}
                title={draft && draft.problems.length > 0 ? t("Fix everything in the issues list first") : undefined}
              >
                {busy === "confirm" ? <Spinner size={13} /> : <CheckCircle2 size={13} />}{t("Confirm workflow")}
              </button>
            </>
          )}
          {readOnly && draft && (
            <button className="btn btn-secondary btn-sm" onClick={() => setTab("deploy")}>
              <Rocket size={13} />{t("Approval & Deploy")}
            </button>
          )}
          {draft && tab === "canvas" && (
            <button
              className="btn btn-ghost btn-sm btn-icon"
              style={{ width: 30, height: 30 }}
              title={t("Agent settings")}
              onClick={() => { setShowSettings((shown) => !shown || Boolean(selectedId)); setSelectedId(null); }}
            >
              <Settings2 size={15} />
            </button>
          )}
        </div>
      </div>

      {/* The detail behind the chip. Same sentences the confirm gate refuses
          on, shown when asked for instead of parked under the canvas. */}
      {showIssues && draft && issueCount > 0 && (
        <div className="ab-float ab-scrollbar" style={{ top: 48, left: 12, width: "min(560px, calc(100% - 24px))", maxHeight: 300, overflowY: "auto", zIndex: 60, flexDirection: "column", gap: 4, padding: "var(--space-2) var(--space-3)" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
            <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".13em", textTransform: "uppercase", color: "var(--color-accent)" }}>{t("Issues to fix")}</p>
            <button className="btn btn-ghost btn-sm btn-icon" style={{ width: 24, height: 24 }} title={t("Close")} onClick={() => setShowIssues(false)}>
              <X size={13} />
            </button>
          </div>
          {draft.problems.map((problem, index) => (
            <p key={`p${index}`} style={{ fontSize: "var(--text-sm)", color: "#f8a5a5", margin: 0, lineHeight: 1.5 }}>· {problem.message}</p>
          ))}
          {draft.warnings.map((warning, index) => (
            <p key={`w${index}`} style={{ fontSize: "var(--text-sm)", color: "var(--ab-warn)", margin: 0, lineHeight: 1.5 }}>· {warning.message}</p>
          ))}
        </div>
      )}

      {/* The frozen-version notice, and the one door out of it. Above the tabs
          rather than on the canvas, because it is true of the whole version —
          the sandbox and the eval tabs are looking at the same frozen graph. */}
      {draft && canFork && !forking && (
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", padding: "var(--space-2) var(--space-3)", borderBottom: "1px solid var(--color-divider)", background: "var(--color-accent-100)", color: "var(--color-accent-800)", flexShrink: 0 }}>
          <GitBranch size={16} style={{ flexShrink: 0 }} />
          <p style={{ margin: 0, flex: 1, minWidth: 240, fontSize: "var(--text-sm)", lineHeight: 1.55 }}>
            {t(FROZEN_BECAUSE[status] || "This version's graph is frozen.")}{" "}
            {t("Editing it creates a new version and leaves this one exactly as it is")}
            {draft.status === "approved" ? t(" — whoever is talking to your published agent keeps talking to this graph.") : "."}
          </p>
          <button className="btn btn-primary" onClick={() => setForking(true)} style={{ flexShrink: 0 }}>
            <GitBranch size={13} />{t("Edit as a new version")}
          </button>
        </div>
      )}

      {draft && canFork && forking && (
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", padding: "var(--space-2) var(--space-3)", borderBottom: "1px solid var(--color-divider)", flexShrink: 0 }}>
          <GitBranch size={16} style={{ flexShrink: 0, color: "var(--color-accent)" }} />
          <p style={{ margin: 0, flex: 1, minWidth: 240, fontSize: "var(--text-sm)", lineHeight: 1.55, color: "color-mix(in srgb, var(--color-text) 70%, transparent)" }}>
            {t("Editing version #{n} as a new version. Nothing is created until your first change is saved — the new version's number appears here when it is.", { n: draft.version_number })}
          </p>
          <button className="btn btn-secondary" onClick={() => setForking(false)} disabled={dirty || !!busy} title={dirty ? t("You have unsaved changes") : undefined} style={{ flexShrink: 0 }}>
            {t("Cancel")}
          </button>
        </div>
      )}

      {error && <div style={{ padding: "var(--space-2) var(--space-5) 0" }}><ErrorNote>{error}</ErrorNote></div>}

      {loading && <div className="ab-empty" style={{ flex: 1 }}><AgentMascot state="exploring" size={92} /></div>}

      {!loading && !draft && ungenerated && (
        <NoWorkflowYet stage={ungenerated} busy={busy === "generate"} onGenerate={generate} />
      )}

      {!loading && draft && tab === "canvas" && (
        <div style={{ flex: 1, position: "relative", minHeight: 0, display: "flex", overflow: "hidden" }}>
          {/* The canvas owns the whole area; palette, inspector and chat float
              over it (Agent Builder's layout). A column each would cost the
              graph a third of the room whether or not anyone was using them. */}
          <BuilderCanvas
            workflow={draft.workflow}
            nodeTypes={nodeTypes}
            readOnly={readOnly}
            selectedId={selectedId}
            setSelectedId={select}
            activeNodeId={activeNodeId}
            changedNodeIds={changedNodeIds}
            problemNodeIds={problemNodeIds}
            onMoveNode={moveNode}
            onAddNodeAtPosition={addNode}
            onConnect={connect}
            onDeleteEdge={deleteEdge}
          />

          {!readOnly && !showPalette && (
            <button
              type="button"
              className="ab-float ab-hide-mobile btn btn-secondary"
              style={{ left: 12, top: 12, height: 34, paddingInline: "var(--space-3)", gap: 6 }}
              aria-expanded={false}
              onClick={() => setShowPalette(true)}
            >
              <Plus size={14} />{t("Add a step")}
            </button>
          )}

          {!readOnly && showPalette && (
            <div className="ab-float ab-float-palette ab-hide-mobile" style={{ width: paletteW }}>
              <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
                {/* The handle, not a heading: the control that opened the
                    palette stays exactly where it was and closes it again. It
                    used to be replaced by the panel, which left the thing you
                    had just clicked gone and a 22px ghost X at the far edge —
                    beside the drag resizer — as the only way out. A disclosure
                    whose trigger disappears on open is a one-way door. */}
                <button
                  type="button"
                  aria-expanded
                  title={t("Close")}
                  onClick={() => setShowPalette(false)}
                  style={{ display: "flex", alignItems: "center", gap: 6, width: "100%", height: 34, flexShrink: 0, paddingInline: "var(--space-3)", border: 0, borderBottom: "1px solid var(--color-divider)", background: "transparent", color: "color-mix(in srgb, var(--color-text) 70%, transparent)", font: "inherit", fontSize: "var(--text-2xs)", letterSpacing: ".13em", textTransform: "uppercase", cursor: "pointer", textAlign: "left" }}
                >
                  <Plus size={14} style={{ flexShrink: 0 }} />
                  <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t("Add a step")}</span>
                  <X size={14} style={{ flexShrink: 0 }} />
                </button>
              <div className="ab-scrollbar" style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "var(--space-2) var(--space-1) var(--space-2) var(--space-1)" }}>
                {PALETTE_ORDER.filter((type) => nodeTypes[type]).map((type) => {
                  const look = lookOf(type);
                  return (
                    <button
                      key={type}
                      type="button"
                      className="ab-palette-row"
                      draggable
                      onDragStart={(e) => e.dataTransfer.setData("application/node-type", type)}
                      onClick={() => addNode(type)}
                      title={t("Add a step (click, or drag onto the canvas)")}
                    >
                      <look.icon size={14} style={{ color: look.color, flexShrink: 0 }} />
                      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t(look.label)}</span>
                      <Plus size={12} style={{ flexShrink: 0, opacity: 0.45 }} />
                    </button>
                  );
                })}
              </div>
              </div>
              <PaneResizer
                size={paletteW}
                onResize={resizePalette}
                onReset={resetPalette}
                grow={1}
                label={t("Drag to resize the palette — double-click to reset")}
              />
            </div>
          )}

          {/* Appears when called for — a selected node, or the header's gear —
              and leaves the graph the whole viewport the rest of the time. */}
          {(selectedNode || showSettings) && (
            <div className="ab-float ab-float-inspector ab-hide-mobile" style={{ width: inspectorW }}>
              <PaneResizer
                size={inspectorW}
                onResize={resizeInspector}
                onReset={resetInspector}
                grow={-1}
                label={t("Drag to resize the inspector — double-click to reset")}
              />
              <div style={{ flex: 1, minWidth: 0, position: "relative", display: "flex", flexDirection: "column" }}>
                <button
                  className="btn btn-ghost btn-sm btn-icon"
                  style={{ position: "absolute", top: 9, right: 9, width: 26, height: 26, zIndex: 5 }}
                  title={t("Close")}
                  onClick={() => select(null)}
                >
                  <X size={14} />
                </button>
                <div className="ab-scrollbar" style={{ flex: 1, overflowY: "auto" }}>
                  <Inspector
                    node={selectedNode}
                    schema={selectedSchema}
                    problems={draft.problems}
                    tools={tools}
                    readOnly={readOnly}
                    isStart={selectedNode ? draft.workflow.start_node_id === selectedNode.id : false}
                    metadata={draft.workflow.metadata}
                    mcpNodeIds={draft.workflow.nodes.filter((item) => item.type === "mcp").map((item) => item.id)}
                    onRename={renameNode}
                    onConfig={setConfig}
                    onDelete={deleteNode}
                    onMakeStart={makeStart}
                    onMetadata={setMetadata}
                    onToolsChanged={reloadTools}
                    notify={notify}
                  />
                </div>
              </div>
            </div>
          )}

          {!readOnly && (
            <div style={{ position: "absolute", left: "50%", transform: "translateX(-50%)", bottom: 14, width: "min(640px, calc(100% - 32px))", zIndex: 12 }}>
              <GraphChat
                agentId={agent.id}
                versionId={versionId}
                workflow={draft.workflow}
                readOnly={readOnly}
                onApply={applyGraph}
                notify={notify}
              />
            </div>
          )}
        </div>
      )}

      {!loading && draft && tab === "sandbox" && (
        <SandboxTab
          agentId={agent.id}
          versionId={versionId}
          notify={notify}
          onActiveNode={setActiveNodeId}
          onVersionChanged={(status) => setDraft((current) => (current ? { ...current, status } : current))}
        />
      )}

      {!loading && draft && tab === "evaluation" && (
        <EvaluationTab agentId={agent.id} versionId={versionId} notify={notify} />
      )}

      {!loading && draft && tab === "deploy" && (
        <DeployTab
          agentId={agent.id}
          versionId={versionId}
          versionStatus={draft.status}
          notify={notify}
          onVersionsChanged={load}
        />
      )}

      {!loading && tab === "operations" && <OperationsTab agentId={agent.id} notify={notify} />}
    </div>
  );
}

/* ============================================================
   The canvas of a version that has no graph yet.

   Reached by opening the builder on a version whose spec is confirmed but whose
   workflow was never generated — from the sidebar, from the agent list, from a
   Generate that failed and was never retried. It used to be the backend's
   sentence in red and nothing else, which is an accurate description of the
   state and a dead end: the button that ends it lived on the spec screen, and
   nothing here said so.

   Two stages reach here and they need opposite things, so the button is only
   drawn for the one it is true for. A spec still being reviewed cannot be
   generated from at all — the API would refuse it — and telling that creator to
   press Generate would be handing them an error.
   ============================================================ */
function NoWorkflowYet({ stage, busy, onGenerate }) {
  const { t } = useLang();
  const ready = stage === "spec_confirmed";
  /* This screen's middle is the agent, at 168px while it composes. The corner
     stands down for as long as that is true. */
  useAgentStandIn();
  return (
    /* `.ab-empty` is a block that centres *inline* content, so the
       `flexDirection` and `gap` here were dead and every `<p>` with a
       `max-width` sat against the left edge while the icon above it looked
       centred. Declaring the flex column the styles already assumed makes both
       true — and the mascot is large enough now that the old skew showed. */
    <div className="ab-empty" style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 14, textAlign: "center", padding: "0 var(--space-6)" }}>
      <AgentMascot state={busy ? "builder" : "idle"} size={busy ? 168 : 116} />
      <p style={{ margin: 0, fontSize: "var(--text-md)", maxWidth: "56ch", lineHeight: 1.6 }}>
        {ready
          ? t("This version's specification is confirmed, but no workflow has been drawn for it yet.")
          : t("This version has no workflow yet — its specification is still being reviewed.")}
      </p>
      {ready ? (
        <>
          <button className="btn btn-primary" onClick={onGenerate} disabled={busy} style={{ height: 38 }}>
            <Layers size={15} />{busy ? t("Composing…") : t("Generate workflow")}
          </button>
          <p style={{ margin: 0, fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)", maxWidth: "56ch", lineHeight: 1.55 }}>
            {t("The platform composes a graph for this specification's own problem. To start from a template instead, pick one on the specification screen.")}
          </p>
        </>
      ) : (
        <p style={{ margin: 0, fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)", maxWidth: "56ch", lineHeight: 1.55 }}>
          {t("Confirm the specification first — the workflow is generated from it.")}
        </p>
      )}
    </div>
  );
}
