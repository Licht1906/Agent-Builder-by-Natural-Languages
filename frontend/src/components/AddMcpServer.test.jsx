import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as api from "../lib/api";
import AddMcpServer, { ConnectorScreen, describeLifetime } from "./AddMcpServer";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    userConnections: { ...actual.userConnections, connectGoogleWithToken: vi.fn(), startGoogle: vi.fn(), list: vi.fn() },
    workflow: { ...actual.workflow, tools: vi.fn() },
    mcpConnections: { ...actual.mcpConnections, mine: vi.fn() },
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

/* The picker says which of the two kinds a tool is: built-in (Gmail, Drive —
 * the platform calls their API, no server to run) or an MCP server an admin
 * approved. A server the creator sent stays on screen while it waits. */
describe("the tool picker", () => {
  const gmailTool = { id: "gmail", name: "Gmail", source: "built_in", actions: [{ name: "send_email" }, { name: "search" }] };
  const crm = { id: "mcp_crm", name: "CRM nội bộ", source: "registered", actions: [{ name: "find_customer" }] };

  function arm({ tools = [gmailTool], mine = [], link = { provider: "google", connected: false } } = {}) {
    api.workflow.tools.mockResolvedValue(tools);
    api.mcpConnections.mine.mockResolvedValue(mine);
    api.userConnections.list.mockResolvedValue([link]);
  }

  it("names both kinds and says whether Google is signed in", async () => {
    arm({ link: { provider: "google", connected: true, account_email: "an@gmail.com", status: "connected" } });
    render(<AddMcpServer open onClose={() => {}} />);

    expect(await screen.findByText("Chọn công cụ")).toBeInTheDocument();
    expect(screen.getByText("Cài sẵn", { selector: "p, p *" })).toBeInTheDocument();
    expect(await screen.findByText("2 hành động · đã nối an@gmail.com")).toBeInTheDocument();
    expect(screen.getByText("MCP server", { selector: "p, p *" })).toBeInTheDocument();
  });

  it("explains, with no MCP server yet, that none has to be built", async () => {
    arm();
    render(<AddMcpServer open onClose={() => {}} />);

    expect(await screen.findByTestId("mcp-empty")).toHaveTextContent(/Không cần tự dựng server/);
  });

  it("shows an approved server to pick and one still waiting on an admin, which cannot be picked", async () => {
    arm({
      tools: [gmailTool, crm],
      mine: [{ id: 3, name: "Kho nội bộ", status: "pending", server_url: "https://kho/mcp" }, { id: 4, name: "CRM nội bộ", status: "approved" }],
    });
    render(<AddMcpServer open onClose={() => {}} />);

    expect(await screen.findByText("1 hành động · đã duyệt")).toBeInTheDocument();
    const waiting = (await screen.findByText("đang chờ admin duyệt")).closest("button");
    expect(waiting).toBeDisabled();
    expect(waiting).toHaveTextContent("Kho nội bộ");
    expect(screen.queryByTestId("mcp-empty")).toBeNull();
  });

  it("labels a connect screen with its kind", () => {
    render(<ConnectorScreen tool={crm} link={null} onBack={() => {}} onUse={() => {}} onConnected={() => {}} notify={() => {}} />);

    expect(screen.getByText("MCP")).toBeInTheDocument();
    expect(screen.getByText(/gọi qua giao thức MCP/)).toBeInTheDocument();
  });
});
