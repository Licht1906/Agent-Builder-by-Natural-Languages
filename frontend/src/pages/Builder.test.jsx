import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import BuilderPage from "./Builder";
import { LanguageProvider } from "../i18n";
import * as api from "../lib/api";

/* The canvas of a version that has no graph.
 *
 * Reported from a real session: a creator finished the interview, confirmed the
 * spec, opened the workflow builder from the sidebar, and got one red line —
 * *"Version này chưa có workflow nào"* — on an otherwise empty screen. The line
 * was true and it was a dead end: the button that ends that state lived on the
 * spec screen, and nothing here said so or offered it.
 *
 * So what is pinned is the way out, not the wording. A confirmed spec is offered
 * the generate it needs; a spec still under review is not offered a button the
 * API would refuse; and a version that does have a graph must not gain either. */

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    workflow: {
      read: vi.fn(),
      generate: vi.fn(),
      save: vi.fn(),
      nodeTypes: vi.fn(),
      tools: vi.fn(),
      templates: vi.fn(),
      confirm: vi.fn(),
    },
    spec: { read: vi.fn() },
    agents: { rename: vi.fn() },
  };
});

const AGENT = { id: 7, name: "Kiểm chéo file trong Drive" };

/** The 404 `GET .../workflow` answers with when the column is still null. */
function noWorkflow() {
  const error = new api.ApiError(404, "Version này chưa có workflow nào", null);
  api.workflow.read.mockRejectedValue(error);
}

const GRAPH = {
  version_id: 12,
  version_number: 1,
  status: "workflow_pending_confirmation",
  problems: [],
  warnings: [],
  workflow: {
    start_node_id: "a",
    nodes: [
      { id: "a", type: "agent_llm", position: { x: 120, y: 80 }, config: { prompt: "Trả lời", model: "gpt-4o-mini" } },
    ],
    edges: [],
    metadata: {},
  },
};

function show() {
  return render(
    <LanguageProvider>
      <BuilderPage agent={AGENT} versionId={12} setVersionId={vi.fn()} notify={vi.fn()} />
    </LanguageProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.workflow.nodeTypes.mockResolvedValue([]);
  api.workflow.tools.mockResolvedValue([]);
});

describe("Builder — a version with no workflow yet", () => {
  it("offers to compose one when the spec is confirmed, instead of only saying there is none", async () => {
    noWorkflow();
    api.spec.read.mockResolvedValue({ status: "spec_confirmed", spec: {}, version_number: 1 });
    api.workflow.generate.mockResolvedValue(GRAPH);
    const user = userEvent.setup();

    show();

    const generate = await screen.findByRole("button", { name: /Generate workflow|Sinh workflow/ });
    await user.click(generate);

    // The same endpoint the spec screen calls, composing rather than filling a
    // template: the creator described a business, not a shape.
    await waitFor(() => expect(api.workflow.generate).toHaveBeenCalledWith(7, 12));
    // And the graph it answered with is on the canvas, without a reload.
    await waitFor(() => expect(screen.queryByRole("button", { name: /Generate workflow/ })).toBeNull());
  });

  it("does not offer a button the API would refuse while the spec is still under review", async () => {
    noWorkflow();
    api.spec.read.mockResolvedValue({ status: "spec_pending_confirmation", spec: {}, version_number: 1 });

    show();

    expect(await screen.findByText(/still being reviewed|đang được soát lại/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Generate workflow|Sinh workflow/ })).toBeNull();
  });

  it("leaves a version that has a graph exactly as it was", async () => {
    api.workflow.read.mockResolvedValue(GRAPH);

    show();

    await waitFor(() => expect(api.workflow.read).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /Generate workflow|Sinh workflow/ })).toBeNull();
    // Nothing asked the spec endpoint anything: the graph was answer enough.
    expect(api.spec.read).not.toHaveBeenCalled();
  });
});

/* The palette over the canvas. Reported by the product owner as "opens but
 * cannot be closed" — which it technically could, through a 22px ghost X at the
 * panel's far edge, next to the drag resizer, after the button they had just
 * pressed vanished. A disclosure whose trigger disappears on open is a one-way
 * door, and "there is a way out if you hunt for it" is not a way out. */
describe("the canvas palette", () => {
  const openCanvas = async () => {
    api.workflow.read.mockResolvedValue(GRAPH);
    api.workflow.nodeTypes.mockResolvedValue([
      { type: "agent_llm", outputs: ["done"], config_schema: {} },
    ]);
    show();
    return screen.findByRole("button", { name: /Add a step|Thêm một bước/i });
  };

  it("closes again from the same control that opened it, in the same place", async () => {
    const actor = userEvent.setup();
    const summon = await openCanvas();
    const before = summon.getBoundingClientRect();

    await actor.click(summon);
    const handle = screen.getByRole("button", { name: /Add a step|Thêm một bước/i });
    expect(handle).toHaveAttribute("aria-expanded", "true");
    /* Same anchor, so the eye does not have to go looking: the trigger becomes
       the panel's handle rather than being replaced by it. */
    expect(handle.getBoundingClientRect().top).toBeCloseTo(before.top, 0);

    await actor.click(handle);
    expect(screen.getByRole("button", { name: /Add a step|Thêm một bước/i }))
      .toHaveAttribute("aria-expanded", "false");
  });

  it("closes on Escape, the reflex for a panel floating over a canvas", async () => {
    const actor = userEvent.setup();
    const summon = await openCanvas();

    await actor.click(summon);
    expect(screen.getByRole("button", { name: /Add a step|Thêm một bước/i })).toHaveAttribute("aria-expanded", "true");

    await actor.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: /Add a step|Thêm một bước/i })).toHaveAttribute("aria-expanded", "false");
  });
});
