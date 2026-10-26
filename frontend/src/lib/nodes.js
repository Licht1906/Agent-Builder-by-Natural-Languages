/* ============================================================
   How the canvas draws each of the backend's ten node types.

   Only what a *picture* needs is here — an icon, a colour, a human label. The
   things that change behaviour, `outputs` and `config_schema`, come from
   `GET /workflow/node-types` at runtime, because `NODE_REGISTRY` on the server
   is the closed table everything else reads (ADR-0021). Hard-coding a port list
   in the browser is how a canvas ends up offering a handle the validator
   refuses, with nothing on screen saying why.
   ============================================================ */
import { Bot, BookOpen, Database, Globe, Plug, GitBranch, RefreshCw, ShieldCheck, Flag, ClipboardList } from "lucide-react";

export const NODE_LOOK = {
  agent_llm: { label: "Agent / LLM", icon: Bot, color: "#7c3aed" },
  knowledge_search: { label: "Knowledge Search", icon: BookOpen, color: "#38bdf8" },
  data_query: { label: "Data Query", icon: Database, color: "#6366f1" },
  web_search: { label: "Web Search", icon: Globe, color: "#0ea5e9" },
  collect: { label: "Collect", icon: ClipboardList, color: "#14b8a6" },
  mcp: { label: "MCP Service", icon: Plug, color: "#f97316" },
  if_else: { label: "If / Else", icon: GitBranch, color: "#f59e0b" },
  while: { label: "While", icon: RefreshCw, color: "#a3e635" },
  human_approval: { label: "Human Approval", icon: ShieldCheck, color: "#ec4899" },
  end: { label: "End", icon: Flag, color: "#22c55e" },
};

const FALLBACK = { label: "Unknown step", icon: Bot, color: "#6b7fa3" };

export const lookOf = (type) => NODE_LOOK[type] || FALLBACK;

/* The order the palette lists them in: the things a step *does* first, the
   control-flow types next, `End` last — which is the order somebody building a
   workflow reaches for them. */
export const PALETTE_ORDER = [
  "agent_llm",
  "knowledge_search",
  "data_query",
  "web_search",
  "collect",
  "mcp",
  "if_else",
  "while",
  "human_approval",
  "end",
];

/* The box a node is drawn in. Agent Builder-sized: one compact row — icon and
   id — because the node on the canvas is a *map marker*, and everything it used
   to also say (the type label, a second line) lives in the inspector a click
   away. `portOffset` below spaces the output handles down `NODE_H`, so both
   numbers are read by the edge geometry as well as by the box. */
export const NODE_W = 168;
export const NODE_H = 44;

/** A node id nothing else in `nodes` is using. Ids are what edges point at, so
 *  a collision is a wire silently attached to the wrong step. */
export function freshNodeId(type, nodes) {
  const taken = new Set(nodes.map((node) => node.id));
  for (let n = 1; ; n += 1) {
    const candidate = `${type}_${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

export function freshEdgeId(edges) {
  const taken = new Set(edges.map((edge) => edge.id));
  for (let n = 1; ; n += 1) {
    const candidate = `edge_${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

/** The blank config a freshly dropped node starts with, read off the type's
 *  JSON Schema so the canvas never invents a field the backend does not have. */
export function blankConfig(schema) {
  const properties = (schema && schema.properties) || {};
  const config = {};
  for (const [name, field] of Object.entries(properties)) {
    config[name] = field.default !== undefined ? field.default : defaultFor(field);
  }
  return config;
}

function defaultFor(field) {
  if (field.type === "array") return [];
  if (field.type === "integer" || field.type === "number") return 0;
  if (field.type === "boolean") return false;
  return "";
}

/** Where an output port sits on a node, so the port dot and the edge that
 *  leaves it are never drawn from two different formulas. */
export function portOffset(index, count) {
  if (count <= 1) return NODE_H / 2;
  return (NODE_H / (count + 1)) * (index + 1);
}

/* ── A graph that was written without a layout ────────────────────────────
   A workflow that arrived through the API (a seed script, an import, an older
   composer) can carry every node at the same point — usually (0,0) — and the
   canvas then draws the whole graph as one pile under the header, which reads
   as "this agent has one node". The document is fine; only the picture lies.

   `spreadStackedPositions` runs on *read* (Builder's `load`) and only when the
   layout is degenerate — two or more nodes all sharing one point — so a graph
   anybody ever dragged is never rearranged. Columns follow the edges out from
   the start node (BFS depth), siblings stack downward, and anything the walk
   cannot reach gets its own column after the deepest one so it stays visible
   rather than under the pile. Positions are display state until the creator
   edits; an edit saves the spread the same way it saves the edit. */
const SPREAD_X0 = 60;
const SPREAD_Y0 = 90;
const SPREAD_DX = NODE_W + 96;
const SPREAD_DY = NODE_H + 60;

export function spreadStackedPositions(workflow) {
  const nodes = (workflow && workflow.nodes) || [];
  if (nodes.length < 2) return workflow;
  const at = (node) => node.position || { x: 0, y: 0 };
  const first = at(nodes[0]);
  if (!nodes.every((node) => at(node).x === first.x && at(node).y === first.y)) return workflow;

  const outgoing = new Map();
  for (const edge of workflow.edges || []) {
    const from = edge.source_node_id;
    if (!outgoing.has(from)) outgoing.set(from, []);
    outgoing.get(from).push(edge.target_node_id);
  }
  const depth = new Map();
  const queue = [];
  if (nodes.some((node) => node.id === workflow.start_node_id)) {
    depth.set(workflow.start_node_id, 0);
    queue.push(workflow.start_node_id);
  }
  while (queue.length) {
    const id = queue.shift();
    for (const next of outgoing.get(id) || []) {
      if (!depth.has(next) && nodes.some((node) => node.id === next)) {
        depth.set(next, depth.get(id) + 1);
        queue.push(next);
      }
    }
  }

  let overflowColumn = 0;
  for (const d of depth.values()) overflowColumn = Math.max(overflowColumn, d + 1);
  const rowsUsed = new Map();
  const placed = nodes.map((node) => {
    const column = depth.has(node.id) ? depth.get(node.id) : overflowColumn++;
    const row = rowsUsed.get(column) || 0;
    rowsUsed.set(column, row + 1);
    return { ...node, position: { x: SPREAD_X0 + column * SPREAD_DX, y: SPREAD_Y0 + row * SPREAD_DY } };
  });
  return { ...workflow, nodes: placed };
}
