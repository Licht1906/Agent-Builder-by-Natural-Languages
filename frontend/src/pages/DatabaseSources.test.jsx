import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import DatabaseSourcesTab, { dsnOf } from "./DatabaseSources";
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

  it("adds a database field by field, composing the DSN, then tests it straight away", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([]);
    api.databaseSources.create.mockResolvedValue(source({ id: 9, status: "untested", views: [] }));
    api.databaseSources.test.mockResolvedValue(source({ id: 9 }));
    draw();
    await screen.findByText(/No database yet/);

    await actor.click(screen.getByRole("button", { name: /Add a database/ }));
    await actor.type(screen.getByLabelText("Name"), "ERP staging");
    await actor.type(screen.getByLabelText("Host"), "erp.noibo");
    await actor.type(screen.getByLabelText("Database name"), "erp");
    const save = screen.getByRole("button", { name: /Save and test/ });
    expect(save).toBeDisabled();
    await actor.type(screen.getByLabelText("Username"), "ro");
    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");
    await actor.type(password, "p@ss/w:rd");
    await actor.click(save);

    await waitFor(() => expect(api.databaseSources.test).toHaveBeenCalledWith(9));
    expect(api.databaseSources.create).toHaveBeenCalledWith({
      name: "ERP staging",
      dialect: "postgresql",
      dsn: "postgresql://ro:p%40ss%2Fw%3Ard@erp.noibo:5432/erp",
    });
  });

  it("adds a SQL Server database by connection string, the box showing the shape that engine takes", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([]);
    api.databaseSources.create.mockResolvedValue(source({ id: 9, dialect: "sqlserver", status: "untested", views: [] }));
    api.databaseSources.test.mockResolvedValue(source({ id: 9, dialect: "sqlserver" }));
    draw();
    await screen.findByText(/No database yet/);

    await actor.click(screen.getByRole("button", { name: /Add a database/ }));
    await actor.type(screen.getByLabelText("Name"), "ERP MSSQL");
    await actor.click(screen.getByRole("radio", { name: /SQL Server/ }));
    expect(screen.getByLabelText("Port")).toHaveAttribute("placeholder", "1433");
    await actor.click(screen.getByRole("radio", { name: "Connection string" }));
    const dsn = screen.getByLabelText("Connection string (DSN)");
    expect(dsn).toHaveAttribute("type", "password");
    expect(dsn).toHaveAttribute("placeholder", expect.stringContaining("Server=host,1433"));
    await actor.type(dsn, "Server=erp,1433;Database=erp;User Id=ro;Password=secret");
    await actor.click(screen.getByRole("button", { name: /Save and test/ }));

    await waitFor(() => expect(api.databaseSources.test).toHaveBeenCalledWith(9));
    expect(api.databaseSources.create).toHaveBeenCalledWith({
      name: "ERP MSSQL",
      dialect: "sqlserver",
      dsn: "Server=erp,1433;Database=erp;User Id=ro;Password=secret",
    });
    expect(await screen.findByText("SQL Server")).toBeInTheDocument();
  });

  it("says beside a MySQL source, and when MySQL is picked, that the scope is filtered in code only", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([source({ dialect: "mysql" }), source({ id: 5, name: "ERP PG" })]);
    draw();

    const card = (await screen.findByText("ERP staging")).closest(".card");
    expect(within(card).getByRole("note")).toHaveTextContent(/only|chỉ/);
    expect(within(screen.getByText("ERP PG").closest(".card")).queryByRole("note")).toBeNull();

    await actor.click(screen.getByRole("button", { name: /Add a database/ }));
    const form = screen.getByRole("radiogroup", { name: "Database type" }).closest("form");
    expect(within(form).queryByRole("note")).toBeNull();
    await actor.click(screen.getByRole("radio", { name: /MySQL/ }));
    expect(within(form).getByRole("note")).toBeInTheDocument();
    await actor.click(screen.getByRole("radio", { name: "Connection string" }));
    expect(screen.getByLabelText("Connection string (DSN)")).toHaveAttribute("placeholder", expect.stringContaining("mysql://"));
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

  it("opens the connection fields straight away on a source that has none", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([
      source({ dialect: "mysql", has_dsn: false, status: "needs_dsn", status_detail: "Chưa có DSN cho Hệ CSDL này — nhập lại DSN", views: [] }),
    ]);
    api.databaseSources.update.mockResolvedValue(source({ status: "untested" }));
    api.databaseSources.test.mockResolvedValue(source());
    draw();

    expect(await screen.findByText(/nhập lại DSN/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Re-enter connection/ })).toBeNull();
    await actor.type(screen.getByLabelText("Host"), "erp");
    await actor.type(screen.getByLabelText("Port"), "3307");
    await actor.type(screen.getByLabelText("Database name"), "erp");
    await actor.type(screen.getByLabelText("Username"), "ro");
    await actor.type(screen.getByLabelText("Password"), "x");
    await actor.click(screen.getByRole("button", { name: /Save and test/ }));

    await waitFor(() => expect(api.databaseSources.update).toHaveBeenCalledWith(4, { dsn: "mysql://ro:x@erp:3307/erp" }));
    await waitFor(() => expect(api.databaseSources.test).toHaveBeenCalledWith(4));
  });

  it("lets a connection be replaced on a source that already has one", async () => {
    const actor = userEvent.setup();
    draw();
    await screen.findByText("ERP staging");

    expect(screen.queryByLabelText("Password")).toBeNull();
    await actor.click(screen.getByRole("button", { name: /Re-enter connection/ }));

    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
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

describe("dsnOf", () => {
  const fields = (overrides) => ({ mode: "fields", host: "erp", port: "", database: "erp", user: "ro", password: "", dsn: "", ...overrides });

  it("uses each dialect's own scheme and default port", () => {
    expect(dsnOf("postgresql", fields())).toBe("postgresql://ro@erp:5432/erp");
    expect(dsnOf("sqlserver", fields())).toBe("mssql://ro@erp:1433/erp");
    expect(dsnOf("mysql", fields({ port: " 3307 " }))).toBe("mysql://ro@erp:3307/erp");
  });

  it("brackets a bare IPv6 host", () => {
    expect(dsnOf("postgresql", fields({ host: "fd00::5" }))).toBe("postgresql://ro@[fd00::5]:5432/erp");
  });

  it("is nothing while a required field is blank or the port is not a number", () => {
    expect(dsnOf("postgresql", fields({ host: " " }))).toBe("");
    expect(dsnOf("postgresql", fields({ user: "" }))).toBe("");
    expect(dsnOf("postgresql", fields({ database: "" }))).toBe("");
    expect(dsnOf("postgresql", fields({ port: "54x" }))).toBe("");
  });

  it("passes a connection string through as typed, trimmed", () => {
    expect(dsnOf("sqlserver", fields({ mode: "dsn", dsn: "  Server=erp;User Id=ro  " }))).toBe("Server=erp;User Id=ro");
  });
});

describe("the tables an agent may change (ADR-0094)", () => {
  const withTables = (overrides) =>
    source({
      tables: [
        { name: "ar_notes", columns: ["id", "customer_id", "note"], writable: false },
        { name: "fin_invoice", columns: ["id", "amount"], writable: false },
      ],
      allowed_write_tables: [],
      ...overrides,
    });

  it("ticks a table for writing and saves it", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([withTables()]);
    api.databaseSources.update.mockResolvedValue(withTables({ allowed_write_tables: ["ar_notes"] }));
    draw();

    const row = (await screen.findByText("ar_notes")).closest("label");
    expect(screen.getByRole("button", { name: /Save writable tables/ })).toBeDisabled();
    await actor.click(within(row).getByRole("checkbox"));
    await actor.click(screen.getByRole("button", { name: /Save writable tables/ }));

    expect(api.databaseSources.update).toHaveBeenCalledWith(4, { allowed_write_tables: ["ar_notes"] });
    expect(screen.getByText(/only after a person approves the exact change/)).toBeInTheDocument();
  });

  it("sets a separate write account field by field, and removes it", async () => {
    const actor = userEvent.setup();
    api.databaseSources.list.mockResolvedValue([withTables()]);
    api.databaseSources.update.mockResolvedValue(withTables({ has_write_dsn: true }));
    draw();
    await screen.findByText("ar_notes");

    await actor.click(screen.getByRole("button", { name: /Set a write account/ }));
    const form = screen.getByTestId("write-account-4");
    await actor.type(within(form).getByLabelText("Host"), "erp");
    await actor.type(within(form).getByLabelText("Database name"), "erp");
    await actor.type(within(form).getByLabelText("Username"), "writer");
    await actor.type(within(form).getByLabelText("Password"), "w");
    await actor.click(within(form).getByRole("button", { name: /Save write account/ }));

    await waitFor(() =>
      expect(api.databaseSources.update).toHaveBeenCalledWith(4, { write_dsn: "postgresql://writer:w@erp:5432/erp" }),
    );
    expect(await screen.findByText(/separate write account\./)).toBeInTheDocument();

    await actor.click(screen.getByRole("button", { name: /Remove write account/ }));
    await waitFor(() => expect(api.databaseSources.update).toHaveBeenCalledWith(4, { write_dsn: "" }));
  });

  it("says so when the database has no base table to offer", async () => {
    api.databaseSources.list.mockResolvedValue([source({ tables: [], allowed_write_tables: [] })]);
    draw();

    expect(await screen.findByText(/No base table found/)).toBeInTheDocument();
  });
});

describe("the description for the agent (ADR-0095)", () => {
  it("saves what the creator writes about the database", async () => {
    const actor = userEvent.setup();
    api.databaseSources.update.mockResolvedValue(source({ agent_notes: "v_ar_open là hoá đơn chưa trả" }));
    draw();
    await screen.findByText("ERP staging");

    await actor.type(screen.getByLabelText(/Describe this database for the agent/), "v_ar_open là hoá đơn chưa trả");
    await actor.click(screen.getByRole("button", { name: /Save description/ }));

    expect(api.databaseSources.update).toHaveBeenCalledWith(4, { agent_notes: "v_ar_open là hoá đơn chưa trả" });
  });
});
