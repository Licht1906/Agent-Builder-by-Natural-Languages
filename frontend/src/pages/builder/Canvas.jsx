import { useEffect, useRef, useState } from "react";
import { AlertCircle, Flag } from "lucide-react";
import { lookOf, NODE_W, NODE_H, portOffset } from "../../lib/nodes";
import { useLang } from "../../i18n";

/** The curve between two ports. Control points reach horizontally out of the
 *  source and into the target — capped, so two neighbouring columns get a
 *  gentle arc rather than a flat line, and a *backwards* edge (a loop, or a
 *  node dragged left of what feeds it) swings wide instead of pinching into
 *  an unreadable knot at the midpoint. */
function edgePath(from, to) {
  const reach = Math.max(48, Math.min(Math.abs(to.x - from.x) / 2, 160));
  return `M ${from.x} ${from.y} C ${from.x + reach} ${from.y}, ${to.x - reach} ${to.y}, ${to.x} ${to.y}`;
}

/* ============================================================
   The Workflow Canvas (#44), editing a real `WorkflowJSON`.

   Everything it draws comes off the document: `nodes[].position` is where a
   node sits (stored, never auto-laid-out — the creator put it there),
   `start_node_id` names the one node marked START, and the handles on a node's
   right edge are the `outputs` the *server's* node registry declares. The one
   thing this component decides for itself is the curve between two ports.
   ============================================================ */
export default function BuilderCanvas({
  workflow,
  nodeTypes,
  readOnly,
  selectedId,
  setSelectedId,
  activeNodeId,
  changedNodeIds,
  problemNodeIds,
  onMoveNode,
  onAddNodeAtPosition,
  onConnect,
  onDeleteEdge,
}) {
  const { t } = useLang();
  const canvasRef = useRef(null);
  const dragState = useRef(null);
  const connectState = useRef(null);
  const panState = useRef(null);
  /* Outlives the pan by one event on purpose: `pointerup` ends the gesture and
     the `click` that decides whether to clear the selection arrives after it. */
  const panMoved = useRef(false);
  const [tempLine, setTempLine] = useState(null);
  const [hoveredEdgeId, setHoveredEdgeId] = useState(null);
  const [panning, setPanning] = useState(false);

  /* Open on the graph, not on the canvas origin. The layout pass centres a
     fresh graph well inside the scrollable extent, so a viewport parked at
     (0,0) shows the empty top-left corner and the creator's first act is
     hunting for their own workflow. Mount-only: after that the scroll position
     is theirs. */
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || workflow.nodes.length === 0) return;
    const xs = workflow.nodes.map((node) => node.position.x);
    const ys = workflow.nodes.map((node) => node.position.y);
    el.scrollLeft = Math.max(0, (Math.min(...xs) + Math.max(...xs) + NODE_W) / 2 - el.clientWidth / 2);
    el.scrollTop = Math.max(0, (Math.min(...ys) + Math.max(...ys) + NODE_H) / 2 - el.clientHeight / 2);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const outputsOf = (type) => (nodeTypes[type] ? nodeTypes[type].outputs : []);
  const nodeById = (id) => workflow.nodes.find((node) => node.id === id);

  const relative = (clientX, clientY) => {
    if (!canvasRef.current) return { x: 80, y: 80 };
    const rect = canvasRef.current.getBoundingClientRect();
    return {
      x: clientX - rect.left + canvasRef.current.scrollLeft,
      y: clientY - rect.top + canvasRef.current.scrollTop,
    };
  };

  const handleDragOver = (event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };

  const handleDrop = (event) => {
    event.preventDefault();
    if (readOnly) return;
    const type = event.dataTransfer.getData("application/node-type");
    if (!type || !onAddNodeAtPosition) return;
    const point = relative(event.clientX, event.clientY);
    onAddNodeAtPosition(type, { x: Math.max(20, point.x - 60), y: Math.max(20, point.y - 20) });
  };

  const onNodePointerDown = (event, node) => {
    setSelectedId(node.id);
    if (readOnly) return;
    event.stopPropagation();
    const point = relative(event.clientX, event.clientY);
    dragState.current = {
      nodeId: node.id,
      offsetX: point.x - node.position.x,
      offsetY: point.y - node.position.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  /* Drag the empty canvas to move the view under it.
   *
   * The palette, the inspector and the chat all float *over* the graph rather
   * than taking a column each, which is what gives a workflow the whole window
   * — and it means a node can end up underneath one of them with no way to get
   * at it. Scrollbars were the only answer and they are the wrong shape for the
   * gesture: you look at a node you cannot reach and reach for the node, not for
   * a bar at the edge of the screen.
   *
   * Mouse only. Touch already pans this element natively, and taking the
   * gesture over would mean re-implementing momentum and rubber-banding to end
   * up where the browser started.
   *
   * `panMoved` is not bookkeeping: `onClick` on this element clears the
   * selection, and a pan ends in a click. Without it, moving the view would
   * silently deselect the node the creator is working on. */
  const onCanvasPointerDown = (event) => {
    if (event.pointerType !== "mouse" || dragState.current || connectState.current) return;
    const el = canvasRef.current;
    if (!el) return;
    panState.current = {
      startX: event.clientX,
      startY: event.clientY,
      scrollLeft: el.scrollLeft,
      scrollTop: el.scrollTop,
    };
    panMoved.current = false;
    setPanning(true);
    // Guarded: jsdom has no pointer capture, and losing it costs a pan that
    // stops at the window edge rather than a crash.
    if (el.setPointerCapture) el.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event) => {
    if (panState.current) {
      const { startX, startY, scrollLeft, scrollTop } = panState.current;
      const dx = event.clientX - startX;
      const dy = event.clientY - startY;
      // Four pixels of slop, so a click that trembles is still a click.
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) panMoved.current = true;
      canvasRef.current.scrollLeft = scrollLeft - dx;
      canvasRef.current.scrollTop = scrollTop - dy;
    }
    if (dragState.current) {
      const point = relative(event.clientX, event.clientY);
      onMoveNode(
        dragState.current.nodeId,
        Math.max(0, point.x - dragState.current.offsetX),
        Math.max(0, point.y - dragState.current.offsetY)
      );
    }
    if (connectState.current) {
      const point = relative(event.clientX, event.clientY);
      setTempLine((line) => (line ? { ...line, x2: point.x, y2: point.y } : line));
    }
  };

  const onOutputPointerDown = (event, node, port) => {
    if (readOnly) return;
    event.stopPropagation();
    connectState.current = { sourceNodeId: node.id, sourcePort: port };
    const point = relative(event.clientX, event.clientY);
    setTempLine({ x1: point.x, y1: point.y, x2: point.x, y2: point.y });
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const finishGesture = (event) => {
    if (connectState.current) {
      const element = document.elementFromPoint(event.clientX, event.clientY);
      const target = element && element.closest && element.closest("[data-input-port]");
      if (target) {
        const targetNodeId = target.getAttribute("data-input-port");
        const { sourceNodeId, sourcePort } = connectState.current;
        if (targetNodeId !== sourceNodeId) onConnect(sourceNodeId, sourcePort, targetNodeId);
      }
      connectState.current = null;
      setTempLine(null);
    }
    dragState.current = null;
    if (panState.current) {
      panState.current = null;
      setPanning(false);
    }
  };

  /* Clearing the selection is what a click on empty canvas means. A pan is not
     that click, however far it went. */
  const onCanvasClick = () => {
    const panned = panMoved.current;
    panMoved.current = false;
    if (!panned) setSelectedId(null);
  };

  const portPoint = (node, port, isOutput) => {
    if (!isOutput) return { x: node.position.x, y: node.position.y + NODE_H / 2 };
    const outputs = outputsOf(node.type);
    const index = Math.max(0, outputs.indexOf(port));
    return { x: node.position.x + NODE_W, y: node.position.y + portOffset(index, outputs.length) };
  };

  /* Big enough that a generated workflow always fits without the creator having
     to hunt for a node that landed off-screen. */
  const extentX = Math.max(1600, ...workflow.nodes.map((node) => node.position.x + NODE_W + 260));
  const extentY = Math.max(900, ...workflow.nodes.map((node) => node.position.y + NODE_H + 200));

  return (
    <div
      ref={canvasRef}
      className={`ab-canvas-wrap ab-scrollbar${panning ? " panning" : ""}`}
      style={{ flex: 1, position: "relative" }}
      onPointerDown={onCanvasPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finishGesture}
      onPointerCancel={finishGesture}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={onCanvasClick}
      title={t("Drag the background to move the view — the palette and the panels float over the graph")}
    >
      <div style={{ width: extentX, height: extentY, position: "relative" }}>
        <svg width={extentX} height={extentY} style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none" }}>
          <defs>
            {/* Direction was previously only implied by left-to-right reading;
                an arrowhead makes it explicit, which is what disambiguates a
                backwards edge from a forwards one crossing it. */}
            <marker id="ab-edge-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto">
              <path d="M 0 1.2 L 9 5 L 0 8.8 z" fill="color-mix(in srgb, var(--color-accent) 75%, transparent)" />
            </marker>
          </defs>
          {workflow.edges.map((edge) => {
            const source = nodeById(edge.source_node_id);
            const target = nodeById(edge.target_node_id);
            // An edge pointing at a deleted node is a legal mid-edit state; the
            // validator reports it, and there is simply nothing to draw.
            if (!source || !target) return null;
            const from = portPoint(source, edge.source_port, true);
            const to = portPoint(target, null, false);
            // Stop short of the port dot so the arrowhead is not buried under it.
            const path = edgePath(from, { x: to.x - 7, y: to.y });
            const hovered = hoveredEdgeId === edge.id;
            return (
              <g key={edge.id}>
                <path
                  d={path}
                  stroke={hovered ? "var(--color-accent)" : "color-mix(in srgb, var(--color-accent) 70%, transparent)"}
                  strokeWidth={hovered ? 3 : 2}
                  fill="none"
                  markerEnd="url(#ab-edge-arrow)"
                />
                {!readOnly && (
                  <path
                    d={path}
                    stroke="transparent"
                    strokeWidth={14}
                    fill="none"
                    style={{ pointerEvents: "stroke", cursor: "pointer" }}
                    onMouseEnter={() => setHoveredEdgeId(edge.id)}
                    onMouseLeave={() => setHoveredEdgeId((current) => (current === edge.id ? null : current))}
                    onClick={(event) => { event.stopPropagation(); onDeleteEdge(edge.id); }}
                  >
                    <title>Click to remove this connection</title>
                  </path>
                )}
              </g>
            );
          })}
          {tempLine && (
            <path
              d={edgePath({ x: tempLine.x1, y: tempLine.y1 }, { x: tempLine.x2, y: tempLine.y2 })}
              stroke="var(--color-accent)"
              strokeWidth={2}
              fill="none"
              strokeDasharray="5 4"
            />
          )}
        </svg>

        {workflow.nodes.map((node) => {
          const look = lookOf(node.type);
          const outputs = outputsOf(node.type);
          const isStart = workflow.start_node_id === node.id;
          const classes = [
            "ab-node",
            selectedId === node.id ? "selected" : "",
            activeNodeId === node.id ? "active-run" : "",
            /* Last of the three marks and the only temporary one: it says "this
               is the box the sentence you just typed was about", and it fades
               on its own rather than competing with a run or a problem. */
            changedNodeIds && changedNodeIds.has(node.id) ? "changed-flag" : "",
            problemNodeIds.has(node.id) ? "error-flag" : "",
          ].filter(Boolean).join(" ");
          return (
            <div
              key={node.id}
              className={classes}
              style={{ left: node.position.x, top: node.position.y, width: NODE_W, height: NODE_H }}
              /* Selecting happens on pointerdown, but the *click* that follows on
                 release still bubbles to the canvas — whose job is to clear the
                 selection. Stopping it here is what makes a node stay selected
                 after the mouse comes up; `stopPropagation` on pointerdown does
                 not cover a different event. */
              onClick={(event) => event.stopPropagation()}
            >
              {isStart && <span className="ab-node-start">START</span>}
              {/* One row, Agent Builder-style: the node is a marker, not a card.
                  The type is said by the icon's colour and shape; the id is the
                  only text, and everything else waits in the inspector. */}
              <div className="ab-node-row" onPointerDown={(event) => onNodePointerDown(event, node)}>
                <div style={{ width: 22, height: 22, borderRadius: 5, background: `${look.color}22`, border: `1px solid ${look.color}44`, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                  <look.icon size={12} style={{ color: look.color }} />
                </div>
                <span className="ab-mono" title={`${look.label} — ${node.id}`} style={{ flex: 1, fontSize: "var(--text-xs)", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{node.id}</span>
                {problemNodeIds.has(node.id) && <AlertCircle size={12} style={{ color: "#f87171", flexShrink: 0 }} />}
                {outputs.length === 0 && <Flag size={10} style={{ color: look.color, opacity: 0.6, flexShrink: 0 }} />}
              </div>

              {/* Every type but the start of the graph accepts incoming edges; an
                  input port has no name and takes any number of them. */}
              <div data-input-port={node.id} className="ab-port" style={{ left: -6, top: NODE_H / 2 - 6 }} />

              {outputs.map((port, index) => (
                <div
                  key={port}
                  className="ab-port"
                  style={{ right: -6, top: portOffset(index, outputs.length) - 6, borderColor: look.color }}
                  onPointerDown={(event) => onOutputPointerDown(event, node, port)}
                  title={port}
                />
              ))}
              {outputs.length > 1 && outputs.map((port, index) => (
                <span
                  key={`${port}-label`}
                  className="ab-mono"
                  style={{ position: "absolute", left: NODE_W + 10, top: portOffset(index, outputs.length) - 6, fontSize: "var(--text-2xs)", color: "var(--ab-muted)", whiteSpace: "nowrap" }}
                >
                  {port}
                </span>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
