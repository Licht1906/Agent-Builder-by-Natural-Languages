import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

import BuilderCanvas from "./Canvas";
import { LanguageProvider } from "../../i18n";

/* Panning the canvas by dragging its background.
 *
 * The palette, the inspector and the chat float *over* the graph rather than
 * taking a column each — that is what gives a workflow the whole window, and it
 * is also how a node ends up underneath one of them. Reported from a real
 * session with a `knowledge_search` node sitting behind the palette: the only
 * way to reach it was a scrollbar at the edge of the screen, which is not where
 * anybody looks when the thing they want is in front of them.
 *
 * What is pinned here is the part that is pure state and would break silently:
 * a pan must not be mistaken for the click that clears the selection, and a real
 * click must still be one. The geometry (how far the view actually travels) is
 * browser behaviour and is checked against a real browser, not jsdom — the same
 * line this suite's config already draws. */

const NODE_TYPES = {
  agent_llm: { type: "agent_llm", outputs: ["done"], config_schema: {} },
  end: { type: "end", outputs: [], config_schema: {} },
};

const WORKFLOW = {
  start_node_id: "a",
  nodes: [
    { id: "a", type: "agent_llm", position: { x: 400, y: 300 }, config: {} },
    { id: "z", type: "end", position: { x: 900, y: 300 }, config: {} },
  ],
  edges: [{ id: "e1", source_node_id: "a", source_port: "done", target_node_id: "z" }],
  metadata: {},
};

function show(overrides = {}) {
  const setSelectedId = vi.fn();
  const result = render(
    <LanguageProvider>
      <BuilderCanvas
        workflow={WORKFLOW}
        nodeTypes={NODE_TYPES}
        readOnly={false}
        selectedId="a"
        setSelectedId={setSelectedId}
        activeNodeId={null}
        changedNodeIds={new Set()}
        problemNodeIds={new Set()}
        onMoveNode={vi.fn()}
        onAddNodeAtPosition={vi.fn()}
        onConnect={vi.fn()}
        onDeleteEdge={vi.fn()}
        {...overrides}
      />
    </LanguageProvider>
  );
  return { setSelectedId, wrap: result.container.querySelector(".ab-canvas-wrap") };
}

/** One mouse drag across the background, `distance` pixels of it. */
function drag(wrap, distance) {
  fireEvent.pointerDown(wrap, { pointerId: 1, pointerType: "mouse", clientX: 600, clientY: 500 });
  fireEvent.pointerMove(wrap, { pointerId: 1, pointerType: "mouse", clientX: 600 - distance, clientY: 500 });
  fireEvent.pointerUp(wrap, { pointerId: 1, pointerType: "mouse", clientX: 600 - distance, clientY: 500 });
  fireEvent.click(wrap);
}

describe("Canvas — dragging the background moves the view", () => {
  it("keeps the selected node selected across a pan", () => {
    const { setSelectedId, wrap } = show();

    drag(wrap, 220);

    // The click that ends a pan must not be read as "the creator clicked away".
    expect(setSelectedId).not.toHaveBeenCalled();
  });

  it("still treats a click that went nowhere as a click", () => {
    const { setSelectedId, wrap } = show();

    // Three pixels: a hand that trembles is still pointing at the background.
    drag(wrap, 3);

    expect(setSelectedId).toHaveBeenCalledWith(null);
  });

  it("says the background is draggable before anyone tries", () => {
    const { wrap } = show();

    expect(wrap.className).toContain("ab-canvas-wrap");
    expect(wrap.getAttribute("title")).toMatch(/Drag the background|Kéo nền/);
  });

  it("leaves touch alone, because this element already pans natively there", () => {
    const { setSelectedId, wrap } = show();

    fireEvent.pointerDown(wrap, { pointerId: 2, pointerType: "touch", clientX: 600, clientY: 500 });
    fireEvent.pointerMove(wrap, { pointerId: 2, pointerType: "touch", clientX: 380, clientY: 500 });
    fireEvent.pointerUp(wrap, { pointerId: 2, pointerType: "touch", clientX: 380, clientY: 500 });
    fireEvent.click(wrap);

    // No pan was ever started, so the click means what it always meant.
    expect(setSelectedId).toHaveBeenCalledWith(null);
  });
});
