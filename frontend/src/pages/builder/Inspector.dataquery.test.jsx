import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import Inspector from "./Inspector";
import { LanguageProvider } from "../../i18n";
import * as api from "../../lib/api";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual("../../lib/api");
  return { ...actual, databaseSources: { list: vi.fn() } };
});

/* DS-04: a `data_query` node's `source_id` and `views` address the creator's
   own Hệ CSDL, so both are picked, never typed — the Hệ CSDL from the
   creator's list, the views from the ones ticked on it. */

/** `NODE_REGISTRY[NodeType.DATA_QUERY].config_schema`, as `GET /workflow/node-types` serves it. */
const DATA_QUERY_SCHEMA = {
  properties: {
    source_id: { default: "", description: "Id của Hệ CSDL node này đọc", resource_ref: true, type: "string" },
    views: { description: "Các view node này được đọc", items: { type: "string" }, resource_ref: true, type: "array" },
    max_rows: { default: 200, description: "Trần số dòng của mỗi lần đọc", maximum: 1000, minimum: 1, optional: true, type: "integer" },
  },
  title: "DataQueryConfig",
  type: "object",
};

const SOURCES = [
  { id: 7, name: "ERP staging", allowed_views: ["v_ar_open", "v_customer_safe"] },
  { id: 9, name: "ERP cũ", allowed_views: [] },
];

function panel(config = {}) {
  const props = {
    node: { id: "query", type: "data_query", config, position: { x: 0, y: 0 } },
    schema: DATA_QUERY_SCHEMA,
    problems: [],
    tools: [],
    readOnly: false,
    isStart: false,
    metadata: { disclaimer: "" },
    onRename: vi.fn(),
    onConfig: vi.fn(),
    onDelete: vi.fn(),
    onMakeStart: vi.fn(),
    onMetadata: vi.fn(),
  };
  render(<LanguageProvider><Inspector {...props} /></LanguageProvider>);
  return props;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.databaseSources.list.mockResolvedValue(SOURCES);
});

describe("Inspector, data_query fields", () => {
  it("picks the Hệ CSDL from the creator's own list", async () => {
    const actor = userEvent.setup();
    const props = panel({ source_id: "", views: [] });

    await actor.click(screen.getByRole("button", { name: /^Database/ }));
    await actor.click(await screen.findByRole("radio", { name: "ERP staging" }));

    expect(props.onConfig).toHaveBeenCalledWith("query", { source_id: "7", views: [] });
  });

  it("drops the views picked on the old Hệ CSDL when the Hệ CSDL changes", async () => {
    const actor = userEvent.setup();
    const props = panel({ source_id: "9", views: ["v_ar_open"] });

    await actor.click(screen.getByRole("button", { name: /^Database/ }));
    await actor.click(await screen.findByRole("radio", { name: "ERP staging" }));

    expect(props.onConfig).toHaveBeenCalledWith("query", { source_id: "7", views: [] });
  });

  it("shows the chosen Hệ CSDL by name", async () => {
    panel({ source_id: "7", views: ["v_ar_open"] });

    expect(await screen.findByText("ERP staging")).toBeInTheDocument();
  });

  it("offers only the views ticked on the chosen Hệ CSDL", async () => {
    const actor = userEvent.setup();
    const props = panel({ source_id: "7", views: [] });
    await waitFor(() => expect(api.databaseSources.list).toHaveBeenCalled());

    await actor.click(screen.getByRole("button", { name: /^Views/ }));
    const boxes = await screen.findAllByRole("checkbox");
    expect(boxes.map((box) => box.getAttribute("aria-label"))).toEqual(["v_ar_open", "v_customer_safe"]);

    await actor.click(screen.getByRole("checkbox", { name: "v_customer_safe" }));
    expect(props.onConfig).toHaveBeenCalledWith("query", { views: ["v_customer_safe"] });
  });

  it("asks for a Hệ CSDL before offering any view", async () => {
    const actor = userEvent.setup();
    panel({ source_id: "", views: [] });

    await actor.click(screen.getByRole("button", { name: /^Views/ }));

    expect(await screen.findByText(/Pick a database first/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("keeps a view that is no longer ticked visible as missing rather than dropping it", async () => {
    const actor = userEvent.setup();
    panel({ source_id: "7", views: ["fin_invoice"] });
    await waitFor(() => expect(api.databaseSources.list).toHaveBeenCalled());

    await actor.click(screen.getByRole("button", { name: /^Views/ }));

    expect(await screen.findByText(/fin_invoice \(not ticked\)/)).toBeInTheDocument();
  });
});
