import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import UC1Connections from "./UC1Connections";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return { ...actual, uc1Connections: { list: vi.fn(), create: vi.fn(), probe: vi.fn() } };
});

const props = {
  session: { email: "agent-admin@example.com", role: "agent_admin" },
  back: () => {},
  onLogout: () => {},
  notify: vi.fn(),
};

const connection = (overrides) => ({
  id: 1,
  name: "erp_readonly",
  type: "postgres",
  credential_ref: "vault://conn/erp_readonly",
  allowed_objects: ["v_ar_open"],
  allowed_hosts: [],
  read_only: true,
  last_probe_status: "never",
  last_probe_detail: null,
  last_probe_objects: [],
  last_probed_at: null,
  created_at: new Date().toISOString(),
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe("UC1Connections", () => {
  it("shows the credential_ref and never a credential", async () => {
    api.uc1Connections.list.mockResolvedValue([connection()]);

    render(<UC1Connections {...props} />);

    expect(await screen.findByText(/vault:\/\/conn\/erp_readonly/)).toBeInTheDocument();
  });

  it("masks the credential field so a shoulder or a screen-share does not read it", async () => {
    api.uc1Connections.list.mockResolvedValue([]);
    render(<UC1Connections {...props} />);
    await waitFor(() => expect(api.uc1Connections.list).toHaveBeenCalled());

    expect(document.querySelector('input[type="password"]')).toBeTruthy();
  });

  it("splits allowed objects on newlines and commas, dropping blanks", async () => {
    const actor = userEvent.setup();
    api.uc1Connections.list.mockResolvedValue([]);
    api.uc1Connections.create.mockResolvedValue(connection());
    render(<UC1Connections {...props} />);
    await waitFor(() => expect(api.uc1Connections.list).toHaveBeenCalled());

    await actor.type(screen.getByPlaceholderText("erp_readonly"), "erp_readonly");
    await actor.type(document.querySelector('input[type="password"]'), "postgresql://u:p@h:5432/db");
    await actor.type(screen.getByPlaceholderText(/v_ar_open/), "v_ar_open,  , v_customer_safe");
    await actor.click(screen.getByRole("button", { name: /Tạo kết nối/ }));

    await waitFor(() =>
      expect(api.uc1Connections.create).toHaveBeenCalledWith(
        expect.objectContaining({ allowed_objects: ["v_ar_open", "v_customer_safe"] })
      )
    );
  });

  it("clears the credential out of the browser the moment it has been sent", async () => {
    const actor = userEvent.setup();
    api.uc1Connections.list.mockResolvedValue([]);
    api.uc1Connections.create.mockResolvedValue(connection());
    render(<UC1Connections {...props} />);
    await waitFor(() => expect(api.uc1Connections.list).toHaveBeenCalled());

    const credential = document.querySelector('input[type="password"]');
    await actor.type(screen.getByPlaceholderText("erp_readonly"), "erp_readonly");
    await actor.type(credential, "postgresql://u:p@h:5432/db");
    await actor.click(screen.getByRole("button", { name: /Tạo kết nối/ }));

    // This form is the only place the secret ever existed in the browser, and
    // it has no reason to outlive the request that carried it.
    await waitFor(() => expect(credential.value).toBe(""));
  });

  it("refuses to submit without a credential", async () => {
    const actor = userEvent.setup();
    api.uc1Connections.list.mockResolvedValue([]);
    render(<UC1Connections {...props} />);
    await waitFor(() => expect(api.uc1Connections.list).toHaveBeenCalled());

    await actor.type(screen.getByPlaceholderText("erp_readonly"), "erp_readonly");

    expect(screen.getByRole("button", { name: /Tạo kết nối/ })).toBeDisabled();
  });

  it("replaces the row with the probe's own answer rather than refetching the list", async () => {
    const actor = userEvent.setup();
    api.uc1Connections.list.mockResolvedValue([connection()]);
    api.uc1Connections.probe.mockResolvedValue(
      connection({ last_probe_status: "connected", last_probe_objects: [{ object: "v_ar_open", row_count: 1284 }] })
    );
    render(<UC1Connections {...props} />);

    await actor.click(await screen.findByRole("button", { name: /Dry-run/ }));

    expect(await screen.findByText(/1284 dòng/)).toBeInTheDocument();
    expect(api.uc1Connections.list).toHaveBeenCalledTimes(1);
  });

  it("shows why a probe failed, not just that it did", async () => {
    api.uc1Connections.list.mockResolvedValue([
      connection({ last_probe_status: "failed", last_probe_detail: "relation v_ar_open does not exist" }),
    ]);

    render(<UC1Connections {...props} />);

    expect(await screen.findByText(/relation v_ar_open does not exist/)).toBeInTheDocument();
  });
});
