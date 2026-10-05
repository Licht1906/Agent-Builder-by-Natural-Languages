import { describe, expect, it } from "vitest";

import { NODE_H, NODE_W, spreadStackedPositions } from "./nodes";

/* What the canvas is protected from: a graph written by a seed script or an
 * import, with every node on one point. The picture must show the graph; the
 * document — and any layout a person ever made — must never be touched. */

const chain = (positions) => ({
  start_node_id: "a",
  nodes: [
    { id: "a", type: "knowledge_search", position: positions.a },
    { id: "b", type: "agent_llm", position: positions.b },
    { id: "c", type: "end", position: positions.c },
  ],
  edges: [
    { id: "e1", source_node_id: "a", source_port: "done", target_node_id: "b" },
    { id: "e2", source_node_id: "b", source_port: "done", target_node_id: "c" },
  ],
});

describe("spreadStackedPositions", () => {
  it("spreads a fully stacked chain into one column per step, in edge order", () => {
    const zero = { x: 0, y: 0 };
    const spread = spreadStackedPositions(chain({ a: zero, b: zero, c: zero }));
    const at = (id) => spread.nodes.find((node) => node.id === id).position;

    // Left to right down the edges, never on top of each other.
    expect(at("a").x).toBeLessThan(at("b").x);
    expect(at("b").x).toBeLessThan(at("c").x);
    expect(at("b").x - at("a").x).toBeGreaterThanOrEqual(NODE_W);
    expect(new Set(spread.nodes.map((node) => `${node.position.x},${node.position.y}`)).size).toBe(3);
  });

  it("keeps a node the walk cannot reach on screen, after the deepest column", () => {
    const zero = { x: 0, y: 0 };
    const graph = chain({ a: zero, b: zero, c: zero });
    graph.nodes.push({ id: "orphan", type: "mcp", position: { x: 0, y: 0 } });
    const spread = spreadStackedPositions(graph);
    const at = (id) => spread.nodes.find((node) => node.id === id).position;
    expect(at("orphan").x).toBeGreaterThan(at("c").x);
  });

  it("never rearranges a graph somebody laid out — even a partly overlapping one", () => {
    const graph = chain({ a: { x: 0, y: 0 }, b: { x: 0, y: 0 }, c: { x: 300, y: 80 } });
    expect(spreadStackedPositions(graph)).toBe(graph);
  });

  it("leaves a single node exactly where it is", () => {
    const single = { start_node_id: "a", nodes: [{ id: "a", type: "end", position: { x: 0, y: 0 } }], edges: [] };
    expect(spreadStackedPositions(single)).toBe(single);
  });

  it("stacks siblings of one column downward by at least a node height", () => {
    const zero = { x: 0, y: 0 };
    const fork = {
      start_node_id: "a",
      nodes: [
        { id: "a", type: "if_else", position: zero },
        { id: "b", type: "agent_llm", position: zero },
        { id: "c", type: "agent_llm", position: zero },
      ],
      edges: [
        { id: "e1", source_node_id: "a", source_port: "true", target_node_id: "b" },
        { id: "e2", source_node_id: "a", source_port: "false", target_node_id: "c" },
      ],
    };
    const spread = spreadStackedPositions(fork);
    const at = (id) => spread.nodes.find((node) => node.id === id).position;
    expect(at("b").x).toBe(at("c").x);
    expect(Math.abs(at("c").y - at("b").y)).toBeGreaterThanOrEqual(NODE_H);
  });
});
