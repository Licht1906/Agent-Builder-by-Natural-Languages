import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import AdminUsers from "./AdminUsers";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return { ...actual, adminUsers: { list: vi.fn(), activate: vi.fn() } };
});

const props = { notify: vi.fn() };

const user = (overrides) => ({
  id: 7,
  email: "pending-hire@example.com",
  role: "creator",
  status: "pending",
  created_at: new Date().toISOString(),
  employee_id: "E007",
  job_title: "Nhân viên kinh doanh",
  department: "SALES",
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AdminUsers", () => {
  it("asks for the pending filter explicitly, never the whole directory", async () => {
    // The tab that means to show a queue and forgets the filter shows
    // everybody, which reads as a far longer queue than there is.
    api.adminUsers.list.mockResolvedValue([]);

    render(<AdminUsers {...props} />);

    await waitFor(() => expect(api.adminUsers.list).toHaveBeenCalledWith("pending"));
  });

  it("drops the filter when the directory tab is chosen", async () => {
    const actor = userEvent.setup();
    api.adminUsers.list.mockResolvedValue([]);
    render(<AdminUsers {...props} />);
    await waitFor(() => expect(api.adminUsers.list).toHaveBeenCalled());

    await actor.click(screen.getByRole("button", { name: /Tất cả tài khoản/ }));

    await waitFor(() => expect(api.adminUsers.list).toHaveBeenLastCalledWith(null));
  });

  it("activates the account whose row was clicked", async () => {
    const actor = userEvent.setup();
    api.adminUsers.list.mockResolvedValue([user()]);
    api.adminUsers.activate.mockResolvedValue({ id: 7, email: "pending-hire@example.com", status: "active" });
    render(<AdminUsers {...props} />);

    await actor.click(await screen.findByRole("button", { name: /Kích hoạt/ }));

    await waitFor(() => expect(api.adminUsers.activate).toHaveBeenCalledWith(7));
  });

  it("offers no activate button for an account that is already active", async () => {
    api.adminUsers.list.mockResolvedValue([user({ status: "active" })]);

    render(<AdminUsers {...props} />);

    expect(await screen.findByText(/đang hoạt động/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Kích hoạt/ })).toBeNull();
  });

  it("says the queue is empty rather than showing a blank page", async () => {
    api.adminUsers.list.mockResolvedValue([]);

    render(<AdminUsers {...props} />);

    expect(await screen.findByText(/Không có ai đang chờ kích hoạt/)).toBeInTheDocument();
  });

  it("surfaces a refusal instead of silently showing nothing", async () => {
    api.adminUsers.list.mockRejectedValue(new Error("Chỉ admin mới truy cập được"));

    render(<AdminUsers {...props} />);

    expect(await screen.findByText("Chỉ admin mới truy cập được")).toBeInTheDocument();
  });
});
