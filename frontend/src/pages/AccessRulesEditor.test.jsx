import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import AccessRulesEditor from "./AccessRulesEditor";
import { LanguageProvider } from "../i18n";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    databaseSources: { accessRules: vi.fn(), saveAccessRules: vi.fn(), accessRulesYaml: vi.fn(), distinctValues: vi.fn() },
  };
});

const source = {
  id: 4,
  allowed_views: ["v_employees", "v_ar_open"],
  views: [
    { name: "v_employees", columns: ["email", "emp_id", "dept_code", "position"], row_count: 3, allowed: true },
    { name: "v_ar_open", columns: ["customer_id", "sales_rep_id", "department"], row_count: 90, allowed: true },
  ],
};

const SAVED = {
  identity: {
    view: "v_employees",
    match_by: "email",
    attributes: { employee_code: "emp_id", department: "dept_code", position: "position" },
  },
  rules: [
    { position: "Nhân viên KD", views: { v_ar_open: { where: { sales_rep_id: "employee_code" } } } },
    { position: "Kế toán trưởng", views: { v_ar_open: "all" } },
  ],
};

const notify = vi.fn();
const draw = () => render(<LanguageProvider><AccessRulesEditor source={source} notify={notify} /></LanguageProvider>);

/** The grant select for one position × view cell. */
const cell = (position, view) => screen.getByLabelText(`${position} — ${view}`);

beforeEach(() => {
  vi.clearAllMocks();
  api.databaseSources.accessRules.mockResolvedValue({ rules: SAVED, problems: [] });
  api.databaseSources.saveAccessRules.mockImplementation(async (_id, rules) => ({ rules, problems: [] }));
});

describe("the access rules editor", () => {
  it("shows the saved rules as one grant per position and view", async () => {
    draw();

    await waitFor(() => expect(cell("Kế toán trưởng", "v_ar_open")).toHaveValue("all"));
    expect(cell("Nhân viên KD", "v_ar_open")).toHaveValue("where");
    const filter = screen.getByTestId("where-Nhân viên KD-v_ar_open");
    expect(within(filter).getByLabelText(/View column/)).toHaveValue("sales_rep_id");
    expect(within(filter).getByLabelText(/Asker attribute/)).toHaveValue("employee_code");
  });

  it("starts from deny-everyone when nothing is saved", async () => {
    api.databaseSources.accessRules.mockResolvedValue({ rules: null, problems: [] });
    draw();

    expect(await screen.findByText(/Nobody can read anything yet/)).toBeInTheDocument();
  });

  it("offers the database's own positions", async () => {
    const actor = userEvent.setup();
    api.databaseSources.distinctValues.mockResolvedValue({ values: ["Kế toán trưởng", "Nhân viên KD", "Trưởng nhóm KD"] });
    draw();
    await waitFor(() => expect(cell("Kế toán trưởng", "v_ar_open")).toBeInTheDocument());

    await actor.click(screen.getByRole("button", { name: /Load positions/ }));

    expect(api.databaseSources.distinctValues).toHaveBeenCalledWith(4, "v_employees", "position");
    expect(await screen.findByLabelText("Trưởng nhóm KD — v_ar_open")).toHaveValue("deny");
  });

  it("saves a filter as a where on the view column and the asker's attribute", async () => {
    const actor = userEvent.setup();
    api.databaseSources.distinctValues.mockResolvedValue({ values: ["Trưởng nhóm KD"] });
    draw();
    await waitFor(() => expect(cell("Kế toán trưởng", "v_ar_open")).toBeInTheDocument());
    await actor.click(screen.getByRole("button", { name: /Load positions/ }));

    await actor.selectOptions(await screen.findByLabelText("Trưởng nhóm KD — v_ar_open"), "where");
    const filter = screen.getByTestId("where-Trưởng nhóm KD-v_ar_open");
    await actor.selectOptions(within(filter).getByLabelText(/View column/), "department");
    await actor.selectOptions(within(filter).getByLabelText(/Asker attribute/), "department");
    await actor.click(screen.getByRole("button", { name: /Save access rules/ }));

    const sent = api.databaseSources.saveAccessRules.mock.calls[0][1];
    expect(sent.identity).toEqual(SAVED.identity);
    expect(sent.rules).toContainEqual({ position: "Trưởng nhóm KD", views: { v_ar_open: { where: { department: "department" } } } });
    expect(sent.rules).toContainEqual({ position: "Kế toán trưởng", views: { v_ar_open: "all" } });
  });

  it("will not save a filter with no complete condition — it would silently mean deny", async () => {
    const actor = userEvent.setup();
    draw();
    await waitFor(() => expect(cell("Kế toán trưởng", "v_ar_open")).toBeInTheDocument());

    await actor.selectOptions(cell("Kế toán trưởng", "v_ar_open"), "where");
    await actor.click(screen.getByRole("button", { name: /Save access rules/ }));

    expect(api.databaseSources.saveAccessRules).not.toHaveBeenCalled();
    expect(await screen.findByText(/Kế toán trưởng — v_ar_open: the filter has no complete condition/)).toBeInTheDocument();
  });

  it("leaves a position whose every view is denied out of the rules", async () => {
    const actor = userEvent.setup();
    draw();
    await waitFor(() => expect(cell("Kế toán trưởng", "v_ar_open")).toBeInTheDocument());

    await actor.selectOptions(cell("Kế toán trưởng", "v_ar_open"), "deny");
    await actor.click(screen.getByRole("button", { name: /Save access rules/ }));

    const sent = api.databaseSources.saveAccessRules.mock.calls[0][1];
    expect(sent.rules.map((rule) => rule.position)).toEqual(["Nhân viên KD"]);
  });

  it("shows every problem the server names when a save is refused", async () => {
    const actor = userEvent.setup();
    api.databaseSources.saveAccessRules.mockRejectedValue(
      Object.assign(new Error("422"), {
        status: 422,
        body: { detail: { message: "Quy tắc truy cập không khớp với Hệ CSDL", problems: ["view v_ar_open không có cột rep_code (quy tắc của chức vụ Nhân viên KD)"] } },
      }),
    );
    draw();
    await waitFor(() => expect(cell("Kế toán trưởng", "v_ar_open")).toBeInTheDocument());

    await actor.click(screen.getByRole("button", { name: /Save access rules/ }));

    expect(await screen.findByText(/không có cột rep_code/)).toBeInTheDocument();
  });

  it("shows the problems saved rules have now", async () => {
    api.databaseSources.accessRules.mockResolvedValue({ rules: SAVED, problems: ["view v_ar_open chưa được tick cho phép (quy tắc của chức vụ Kế toán trưởng)"] });
    draw();

    expect(await screen.findByText(/chưa được tick cho phép/)).toBeInTheDocument();
  });

  it("shows the rules as YAML", async () => {
    const actor = userEvent.setup();
    api.databaseSources.accessRulesYaml.mockResolvedValue("identity:\n  view: v_employees\n");
    draw();
    await waitFor(() => expect(cell("Kế toán trưởng", "v_ar_open")).toBeInTheDocument());

    await actor.click(screen.getByRole("button", { name: /View YAML/ }));

    expect(await screen.findByText(/view: v_employees/)).toBeInTheDocument();
  });
});
