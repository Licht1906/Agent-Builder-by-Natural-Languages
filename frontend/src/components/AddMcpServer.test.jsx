import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as api from "../lib/api";
import { ConnectorScreen, describeLifetime } from "./AddMcpServer";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    userConnections: { ...actual.userConnections, connectGoogleWithToken: vi.fn(), startGoogle: vi.fn() },
  };
});

/* A pasted Google token dies within the hour. The panel turns the backend's
 * `expires_at` — naive UTC, no offset — into "còn khoảng N phút" or "đã hết
 * hạn", and must read that string as UTC rather than as the browser's clock. */
describe("describeLifetime", () => {
  const now = Date.parse("2026-08-22T05:00:00Z");

  it("has nothing to say for the sign-in path", () => {
    expect(describeLifetime(null, now)).toBeNull();
    expect(describeLifetime("", now)).toBeNull();
  });

  it("reads a naive timestamp as UTC and counts the minutes left", () => {
    expect(describeLifetime("2026-08-22T05:52:30", now)).toEqual({ expired: false, seconds: 3150, label: "53 phút" });
  });

  it("accepts an explicit offset too", () => {
    expect(describeLifetime("2026-08-22T12:10:00+07:00", now).label).toBe("10 phút");
    expect(describeLifetime("2026-08-22T13:10:00+07:00", now).label).toBe("1 giờ 10 phút");
  });

  it("calls a past expiry expired", () => {
    const gone = describeLifetime("2026-08-22T04:59:00", now);
    expect(gone.expired).toBe(true);
    expect(gone.label).toBe("đã hết hạn");
  });
});

/* The paste box is always on screen for Gmail/Drive. Two earlier versions hid
 * it — behind "đang nối", then behind a "Đổi token" toggle — and a creator who
 * chose Gmail a second time found nowhere to paste the fresh token the new
 * agent needed. */
describe("ConnectorScreen — per-user connector", () => {
  const gmail = { id: "gmail", name: "Gmail", actions: [{ name: "send_email", risk_level: "high" }] };
  const live = { provider: "google", connected: true, token_kind: "access_token", status: "connected", expires_at: new Date(Date.now() + 50 * 60000).toISOString() };

  it("đã nối vẫn có nút đăng nhập và ô dán token", () => {
    render(<ConnectorScreen tool={gmail} link={live} onBack={() => {}} onUse={() => {}} onConnected={() => {}} notify={() => {}} />);

    expect(screen.getByPlaceholderText("Dán access token")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Đăng nhập Google/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dùng token mới" })).toBeDisabled();
    expect(screen.getByText(/còn khoảng/)).toBeInTheDocument();
  });

  it("dán token khác khi đang nối thì gửi token đó lên, rồi xoá ô", async () => {
    api.userConnections.connectGoogleWithToken.mockResolvedValue({ account_email: "", token_kind: "access_token" });
    const onConnected = vi.fn();
    render(<ConnectorScreen tool={gmail} link={live} onBack={() => {}} onUse={() => {}} onConnected={onConnected} notify={() => {}} />);

    const box = screen.getByPlaceholderText("Dán access token");
    await userEvent.type(box, "ya29.moi");
    await userEvent.click(screen.getByRole("button", { name: "Dùng token mới" }));

    expect(api.userConnections.connectGoogleWithToken).toHaveBeenCalledWith("ya29.moi");
    expect(onConnected).toHaveBeenCalled();
    expect(box).toHaveValue("");
  });

  it("chọn lại Gmail lần nữa — dựng lại màn hình — ô dán vẫn ở đó", () => {
    const first = render(<ConnectorScreen tool={gmail} link={live} onBack={() => {}} onUse={() => {}} onConnected={() => {}} notify={() => {}} />);
    first.unmount();
    render(<ConnectorScreen tool={gmail} link={live} onBack={() => {}} onUse={() => {}} onConnected={() => {}} notify={() => {}} />);

    expect(screen.getByPlaceholderText("Dán access token")).toBeInTheDocument();
  });

  it("token hết hạn: nói rõ, ô dán vẫn đó", () => {
    const dead = { ...live, status: "needs_reconnect", expires_at: new Date(Date.now() - 60000).toISOString() };
    render(<ConnectorScreen tool={gmail} link={dead} onBack={() => {}} onUse={() => {}} onConnected={() => {}} notify={() => {}} />);

    expect(screen.getByText(/token đã hết hạn/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Dán access token")).toBeInTheDocument();
  });
});
