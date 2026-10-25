import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import DatabaseSourcesTab from "./DatabaseSources";
import { LanguageProvider } from "../i18n";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    databaseSources: {
      list: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      test: vi.fn(),
      remove: vi.fn(),
      accessRules: vi.fn(),
    },
  };
});

const source = (overrides) => ({
  id: 4,
  name: "ERP staging",
  dialect: "postgresql",
  has_dsn: true,
  status: "connected",
  status_reason: null,
  status_detail: "Kết nối thành công — 2 view",
  last_probed_at: new Date().toISOString(),
  allowed_views: ["v_ar_open"],
  views: [
    { name: "v_ar_open", columns: ["customer_id", "days_overdue"], row_count: 1284, allowed: true },
    { name: "v_customer_safe", columns: ["customer_id"], row_count: 96, allowed: false },
  ],
  base_tables: ["fin_invoice"],
  created_at: new Date().toISOString(),
  ...overrides,
});

const notify = vi.fn();
const draw = () => render(<LanguageProvider><DatabaseSourcesTab tabs={null} notify={notify} /></LanguageProvider>);

beforeEach(() => {
  vi.clearAllMocks();
  api.databaseSources.list.mockResolvedValue([source()]);
});

describe("the Databases tab", () => {
  it("shows each Hệ CSDL with its views and their row counts", async () => {
    draw();

    expect(await screen.findByText("ERP staging")).toBeInTheDocument();
    const row = screen.getByText("v_ar_open").closest("label");
    expect(within(row).getByText(/1[.,]?284/)).toBeInTheDocument();
    expect(within(row).getByRole("checkbox")).toBeChecked();
    expect(screen.getByText(/fin_invoice/)).toBeInTheDocument();
  });

  it("lists a view it could not count without inventing a number", async () => {
    api.databaseSources.list.mockResolvedValue([
      source({ views: [{ name: "v_slow", columns: ["x"], row_count: null, allowed: false }] }),
    ]);
    draw();

    const row = (await screen.findByText("v_slow")).closest("label");
    expect(within(row).getByText(/rows not counted/)).toBeInTheDocument();
  });

  it("says when there is nothing registered yet", async () => {
    api.databaseSources.list.mockResolvedValue([]);
    draw();

    expect(await screen.findByText(/No database yet/)).toBeInTheDocument();
  });

  it("adds a database with its DSN in a password box, then tests it straight away", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([]);
    api.databaseSources.create.mockResolvedValue(source({ id: 9, status: "untested", views: [] }));
    api.databaseSources.test.mockResolvedValue(source({ id: 9 }));
    draw();
    await screen.findByText(/No database yet/);

    await actor.click(screen.getByRole("button", { name: /Add a database/ }));
    await actor.type(screen.getByLabelText(/Name/), "ERP staging");
    const dsn = screen.getByLabelText(/DSN/);
    expect(dsn).toHaveAttribute("type", "password");
    await actor.type(dsn, "postgresql://ro:secret@erp:5432/erp");
    await actor.click(screen.getByRole("button", { name: /Save and test/ }));

    await waitFor(() => expect(api.databaseSources.test).toHaveBeenCalledWith(9));
    expect(api.databaseSources.create).toHaveBeenCalledWith({
      name: "ERP staging",
      dialect: "postgresql",
      dsn: "postgresql://ro:secret@erp:5432/erp",
    });
  });

  it("saves the views that are ticked", async () => {
    const actor = userEvent.setup();
    api.databaseSources.update.mockResolvedValue(source({ allowed_views: ["v_ar_open", "v_customer_safe"] }));
    draw();
    await screen.findByText("ERP staging");

    await actor.click(within(screen.getByText("v_customer_safe").closest("label")).getByRole("checkbox"));
    await actor.click(screen.getByRole("button", { name: /Save allowed views/ }));

    expect(api.databaseSources.update).toHaveBeenCalledWith(4, { allowed_views: ["v_ar_open", "v_customer_safe"] });
  });

  it("opens the DSN box straight away on a source that has none", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([
      source({ has_dsn: false, status: "needs_dsn", status_detail: "Chưa có DSN cho Hệ CSDL này — nhập lại DSN", views: [] }),
    ]);
    api.databaseSources.update.mockResolvedValue(source({ status: "untested" }));
    api.databaseSources.test.mockResolvedValue(source());
    draw();

    expect(await screen.findByText(/nhập lại DSN/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Re-enter DSN/ })).toBeNull();
    await actor.type(screen.getByLabelText(/New DSN/), "postgresql://ro:x@erp/erp");
    await actor.click(screen.getByRole("button", { name: /Save DSN/ }));

    await waitFor(() => expect(api.databaseSources.update).toHaveBeenCalledWith(4, { dsn: "postgresql://ro:x@erp/erp" }));
    await waitFor(() => expect(api.databaseSources.test).toHaveBeenCalledWith(4));
  });

  it("lets a DSN be replaced on a source that already has one", async () => {
    const actor = userEvent.setup();
    draw();
    await screen.findByText("ERP staging");

    expect(screen.queryByLabelText(/New DSN/)).toBeNull();
    await actor.click(screen.getByRole("button", { name: /Re-enter DSN/ }));

    expect(screen.getByLabelText(/New DSN/)).toHaveAttribute("type", "password");
  });

  it("opens the access rules of a source that has ticked views", async () => {
    const actor = userEvent.setup();
    api.databaseSources.accessRules.mockResolvedValue({ rules: null, problems: [] });
    draw();
    await screen.findByText("ERP staging");

    expect(api.databaseSources.accessRules).not.toHaveBeenCalled();
    await actor.click(screen.getByRole("button", { name: /Access rules/ }));

    expect(await screen.findByText(/Nobody can read anything yet/)).toBeInTheDocument();
    expect(api.databaseSources.accessRules).toHaveBeenCalledWith(4);
  });

  it("shows why a delete was refused", async () => {
    const actor = userEvent.setup();
    const refusal = Object.assign(new Error("conflict"), {
      status: 409,
      body: { detail: { message: "Không thể xoá — Hệ CSDL này đang được dùng bởi agent: Trợ lý công nợ", agents: ["Trợ lý công nợ"] } },
    });
    api.databaseSources.remove.mockRejectedValue(refusal);
    draw();
    await screen.findByText("ERP staging");

    await actor.click(screen.getByRole("button", { name: /Delete/ }));

    await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.stringMatching(/Trợ lý công nợ/), "error"));
  });
});
