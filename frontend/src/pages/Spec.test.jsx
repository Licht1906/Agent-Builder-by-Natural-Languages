import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import SpecPage from "./Spec";
import { LanguageProvider } from "../i18n";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    spec: { read: vi.fn(), revise: vi.fn(), confirm: vi.fn() },
    workflow: { read: vi.fn(), templates: vi.fn(), generate: vi.fn() },
    knowledgeBases: { list: vi.fn(), create: vi.fn() },
    databaseSources: { list: vi.fn() },
    agents: { setKnowledgeBases: vi.fn() },
  };
});

const specWith = (data) => ({
  goal: "Trả lời câu hỏi công nợ",
  users: ["Nhân viên kinh doanh"],
  data,
  actions: ["Tra công nợ"],
  expected_outcome: ["Câu trả lời có số liệu"],
  success_criteria: [{ text: "Đúng số liệu", category: "correctness" }],
  prohibitions: ["Không xem khách của người khác"],
});

const open = (data) => {
  api.spec.read.mockResolvedValue({ spec: specWith(data), status: "spec_pending_confirmation", version_number: 1 });
  render(
    <LanguageProvider>
      <SpecPage agentId={1} versionId={2} agentName="Trợ lý công nợ" openBuilder={vi.fn()} notify={vi.fn()} />
    </LanguageProvider>,
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  api.workflow.read.mockRejectedValue(new Error("404"));
  api.workflow.templates.mockResolvedValue([]);
  api.knowledgeBases.list.mockResolvedValue([]);
  api.databaseSources.list.mockResolvedValue([]);
  api.spec.revise.mockImplementation(async (_agent, _version, body) => ({ spec: body, status: "spec_pending_confirmation" }));
});

/* DS-01: a `database` source is addressed like a Knowledge Base — picked, never
   typed — and until one is picked the screen says so. */
describe("a database data source on the spec screen", () => {
  it("is offered as a kind", async () => {
    open([{ name: "ERP công nợ", kind: "database", ref: null }]);

    const kind = await screen.findByDisplayValue("Database");
    expect(kind.tagName).toBe("SELECT");
  });

  it("says it is not attached yet, and offers no free-text address box", async () => {
    open([{ name: "ERP công nợ", kind: "database", ref: null }]);

    expect(await screen.findByText(/Not attached yet/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Connection id")).toBeNull();
    expect(screen.getByText(/No database registered yet/)).toBeInTheDocument();
  });

  it("keeps a database ref it cannot find visible rather than dropping it", async () => {
    open([{ name: "ERP công nợ", kind: "database", ref: "12" }]);

    expect(await screen.findByText(/Database #12 \(missing\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Not attached yet/)).toBeNull();
  });

  it("offers the creator's own Hệ CSDL and saves the one picked as the ref", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([{ id: 7, name: "ERP staging" }]);
    open([{ name: "ERP công nợ", kind: "database", ref: null }]);

    await actor.click((await screen.findByText("ERP staging")).closest("button"));
    await actor.click(screen.getByRole("button", { name: /Save draft/ }));

    expect(screen.queryByText(/Not attached yet/)).toBeNull();
    expect(api.spec.revise.mock.calls[0][2].data).toEqual([{ name: "ERP công nợ", kind: "database", ref: "7" }]);
  });

  it("saves the source as database with no ref", async () => {
    const actor = userEvent.setup();
    open([{ name: "ERP công nợ", kind: "database", ref: null }]);
    await screen.findByText(/Not attached yet/);

    await actor.click(screen.getByRole("button", { name: /Save draft/ }));

    const body = api.spec.revise.mock.calls[0][2];
    expect(body.data).toEqual([{ name: "ERP công nợ", kind: "database", ref: null }]);
  });
});
