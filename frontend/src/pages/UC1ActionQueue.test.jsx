import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import UC1ActionQueue from "./UC1ActionQueue";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return { ...actual, uc1Actions: { queue: vi.fn(), decide: vi.fn() } };
});

const props = {
  session: { email: "manager@example.com", role: "sales_manager" },
  back: () => {},
  onLogout: () => {},
  notify: vi.fn(),
};

const action = (overrides) => ({
  action_hash: "a1b2c3",
  agent_id: "agt_ar",
  version: 2,
  tool: "mail.send_reminder",
  args: { to: "khach@corp.vn", amount: 1500000 },
  approver_roles: ["sales_manager"],
  requested_by_employee_id: "E007",
  created_at: new Date().toISOString(),
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe("UC1ActionQueue", () => {
  it("renders the arguments in full — approving authorises exactly this call", async () => {
    api.uc1Actions.queue.mockResolvedValue([action()]);

    render(<UC1ActionQueue {...props} />);

    // Not a summary and not the tool name alone: the recipient and the amount
    // are the whole of what is being authorised.
    expect(await screen.findByText(/khach@corp\.vn/)).toBeInTheDocument();
    expect(screen.getByText(/1500000/)).toBeInTheDocument();
  });

  it("passes the decision and the typed reason through", async () => {
    const actor = userEvent.setup();
    api.uc1Actions.queue.mockResolvedValue([action()]);
    api.uc1Actions.decide.mockResolvedValue({ action_hash: "a1b2c3", tool: "mail.send_reminder", status: "rejected", reasons: [] });
    render(<UC1ActionQueue {...props} />);

    await actor.type(await screen.findByPlaceholderText(/Lý do/), "sai khách hàng");
    await actor.click(screen.getByRole("button", { name: /Từ chối/ }));

    await waitFor(() => expect(api.uc1Actions.decide).toHaveBeenCalledWith("a1b2c3", "reject", "sai khách hàng"));
  });

  it("sends null rather than an empty string when no reason was typed", async () => {
    const actor = userEvent.setup();
    api.uc1Actions.queue.mockResolvedValue([action()]);
    api.uc1Actions.decide.mockResolvedValue({ action_hash: "a1b2c3", tool: "mail.send_reminder", status: "executed", reasons: [] });
    render(<UC1ActionQueue {...props} />);

    await actor.click(await screen.findByRole("button", { name: /Duyệt & thực thi/ }));

    await waitFor(() => expect(api.uc1Actions.decide).toHaveBeenCalledWith("a1b2c3", "approve", null));
  });

  it("shows the queue empty rather than a blank page", async () => {
    api.uc1Actions.queue.mockResolvedValue([]);

    render(<UC1ActionQueue {...props} />);

    expect(await screen.findByText(/Không có hành động nào đang chờ/)).toBeInTheDocument();
  });

  it("names the roles allowed to decide, so a reader knows whether it is theirs", async () => {
    api.uc1Actions.queue.mockResolvedValue([action({ approver_roles: ["sales_manager", "finance_lead"] })]);

    render(<UC1ActionQueue {...props} />);

    expect(await screen.findByText("sales_manager")).toBeInTheDocument();
    expect(screen.getByText("finance_lead")).toBeInTheDocument();
  });
});
