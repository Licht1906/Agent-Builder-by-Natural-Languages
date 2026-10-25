import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import KnowledgeBasesPage from "./KnowledgeBases";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return { ...actual, knowledgeBases: { list: vi.fn() }, databaseSources: { list: vi.fn() } };
});

const shelf = {
  id: 3,
  name: "Sổ tay khiếu nại",
  mode: "managed",
  document_count: 2,
  agent_names: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.knowledgeBases.list.mockResolvedValue([shelf]);
  api.databaseSources.list.mockResolvedValue([]);
});

/* DS-01 / ADR-0059 §2: "Nguồn dữ liệu" is one page with two kinds on it —
   the Knowledge Bases that were the whole page before, and Hệ CSDL. */
describe("the Data sources page", () => {
  it("opens on the Documents tab, which is still the Knowledge Base list", async () => {
    render(<KnowledgeBasesPage notify={vi.fn()} />);

    expect(screen.getByRole("tab", { name: /Documents/ })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Sổ tay khiếu nại")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create a Knowledge Base/ })).toBeInTheDocument();
  });

  it("has a Databases tab with an empty state and an Add button", async () => {
    const actor = userEvent.setup();
    render(<KnowledgeBasesPage notify={vi.fn()} />);
    await screen.findByText("Sổ tay khiếu nại");

    await actor.click(screen.getByRole("tab", { name: /Databases/ }));

    expect(screen.getByRole("tab", { name: /Databases/ })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText(/No database yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add a database/ })).toBeInTheDocument();
    expect(screen.queryByText("Sổ tay khiếu nại")).toBeNull();
  });

  it("comes back to the shelves when Documents is picked again", async () => {
    const actor = userEvent.setup();
    render(<KnowledgeBasesPage notify={vi.fn()} />);
    await screen.findByText("Sổ tay khiếu nại");

    await actor.click(screen.getByRole("tab", { name: /Databases/ }));
    await actor.click(screen.getByRole("tab", { name: /Documents/ }));

    await waitFor(() => expect(screen.getByText("Sổ tay khiếu nại")).toBeInTheDocument());
  });
});
