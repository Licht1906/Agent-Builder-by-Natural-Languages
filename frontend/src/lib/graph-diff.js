import { lookOf } from "./nodes";

/* ============================================================
   What changed between two graphs, and how to say it in one line.

   Computed here rather than asked of the meta-agent. A model can be asked to
   describe its own edit and will describe the edit it *meant* to make, which is
   the one description a creator cannot check. A diff of the two documents is the
   edit that actually arrived.
   ============================================================ */

/** An edge by what it means, which is not what it is keyed by.
 *
 *  `edge.id` is minted by the canvas and carries nothing: re-drawing a
 *  connection between the same two ports produces a new id for a graph that
 *  behaves identically, and diffing on it would report a rewiring that never
 *  happened. The backend's own version comparison uses this same triple. */
const edgeRef = (edge) => `${edge.source_node_id}·${edge.source_port}·${edge.target_node_id}`;

const byId = (nodes) => new Map(nodes.map((node) => [node.id, node]));

export function diffGraphs(before, after) {
  const was = byId(before.nodes);
  const now = byId(after.nodes);

  const addedNodes = after.nodes.filter((node) => !was.has(node.id));
  const removedNodes = before.nodes.filter((node) => !now.has(node.id));
  const retypedNodes = after.nodes.filter((node) => was.has(node.id) && was.get(node.id).type !== node.type);
  const changedConfigNodes = after.nodes.filter(
    (node) =>
      was.has(node.id) &&
      was.get(node.id).type === node.type &&
      JSON.stringify(was.get(node.id).config) !== JSON.stringify(node.config)
  );

  const wasEdges = new Set(before.edges.map(edgeRef));
  const nowEdges = new Set(after.edges.map(edgeRef));
  const addedEdges = after.edges.filter((edge) => !wasEdges.has(edgeRef(edge)));
  const removedEdges = before.edges.filter((edge) => !nowEdges.has(edgeRef(edge)));

  const startMoved = before.start_node_id !== after.start_node_id;

  /* Only nodes that still exist can be lit up — a removed one has no box left to
     ring. Both ends of a new edge count as touched, because a rewiring is a
     change to the two steps it joins, and those are what the creator looks at. */
  const touchedNodeIds = new Set([
    ...addedNodes.map((node) => node.id),
    ...retypedNodes.map((node) => node.id),
    ...changedConfigNodes.map((node) => node.id),
    ...addedEdges.flatMap((edge) => [edge.source_node_id, edge.target_node_id]),
    ...removedEdges.flatMap((edge) => [edge.source_node_id, edge.target_node_id]),
  ].filter((id) => now.has(id)));

  return {
    addedNodes,
    removedNodes,
    retypedNodes,
    changedConfigNodes,
    addedEdges,
    removedEdges,
    startMoved,
    touchedNodeIds,
    empty:
      addedNodes.length === 0 &&
      removedNodes.length === 0 &&
      retypedNodes.length === 0 &&
      changedConfigNodes.length === 0 &&
      addedEdges.length === 0 &&
      removedEdges.length === 0 &&
      !startMoved,
  };
}

const names = (nodes) => nodes.map((node) => node.id).join(", ");

/** The diff as sentences a creator reads, most structural change first.
 *
 *  Returned as a list rather than one string so the bubble can put each on its
 *  own line — "added a step" and "rewrote two prompts" are two facts, and a
 *  creator scanning for the first should not have to read past the second. */
export function describeDiff(diff) {
  const lines = [];
  if (diff.addedNodes.length) {
    lines.push(`Thêm ${diff.addedNodes.map((node) => `${lookOf(node.type).label} (${node.id})`).join(", ")}`);
  }
  if (diff.removedNodes.length) lines.push(`Bỏ ${names(diff.removedNodes)}`);
  if (diff.retypedNodes.length) lines.push(`Đổi loại ${names(diff.retypedNodes)}`);
  if (diff.addedEdges.length) {
    lines.push(`Nối ${diff.addedEdges.map((edge) => `${edge.source_node_id} → ${edge.target_node_id}`).join(", ")}`);
  }
  if (diff.removedEdges.length) {
    lines.push(`Bỏ nối ${diff.removedEdges.map((edge) => `${edge.source_node_id} → ${edge.target_node_id}`).join(", ")}`);
  }
  if (diff.changedConfigNodes.length) lines.push(`Sửa nội dung ${names(diff.changedConfigNodes)}`);
  if (diff.startMoved) lines.push("Đổi bước bắt đầu");
  /* A model that answered with the graph it was given is not a failure and not
     an error — it is an answer, and saying so is more use than an empty bubble. */
  return lines.length ? lines : ["Không có gì thay đổi — thử nói cụ thể hơn xem"];
}
