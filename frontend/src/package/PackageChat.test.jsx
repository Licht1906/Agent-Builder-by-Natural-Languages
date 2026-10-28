import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PackageChat from "./PackageChat";
import { LanguageProvider } from "../i18n";

/* The exported package's page against a stubbed runtime: the ticket's path —
   signed in → asks → sees the sources → approves the drafted mail — at the
   requests the page makes. The runtime half of the same path (OIDC issuer,
   MailHog) is `tests/test_standalone/test_package_ui.py`. */

const OIDC_CONFIG = {
  agent: "Trợ lý công nợ",
  auth: { mode: "oidc", oidc: { issuer: "https://idp.cty.test", client_id: "tro-ly-cong-no", scope: "openid email profile" } },
};
const REMINDER = { to: "khach@example.com", subject: "Nhắc công nợ quá hạn", body: "Hoá đơn C1 đã quá hạn 45 ngày." };

function tokenWith(claims) {
  const part = (value) => btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${part({ alg: "RS256" })}.${part(claims)}.sig`;
}
const TOKEN = tokenWith({ email: "an@cty.vn", exp: Math.floor(Date.now() / 1000) + 600 });

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const sse = (...events) =>
  new Response(events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });

const FIRST_LEG = [
  ["thread", { conversation_id: "c-1", namespace: "production", trace_id: "t-1" }],
  ["node_started", { node_id: "query", node_type: "data_query" }],
  ["tool", { node_id: "query", tool: "data_query", args: {}, simulated: false }],
  [
    "tool_result",
    {
      node_id: "query",
      tool: "data_query",
      ok: true,
      result: "1 dòng",
      citations: [{ doc_name: "ERP mẫu · v_sa_ar", section_path: "ERP mẫu › v_sa_ar", page_number: null, text: "customer_id=C1, days_overdue=45" }],
    },
  ],
  ["node_started", { node_id: "draft", node_type: "agent_llm" }],
  ["node", { node_id: "draft", node_type: "agent_llm", content: "Khách C1 quá hạn 45 ngày — đã soạn mail nhắc." }],
  [
    "awaiting_approval",
    {
      approval_id: 42,
      node_id: "approval",
      approver_role: "manager",
      message: "Gửi mail nhắc nợ này?",
      actions: [{ node_id: "send", tool: "gmail.send_email", action: "send_email", args: REMINDER, hash: "h" }],
    },
  ],
];

function runtime(routes) {
  return vi.fn(async (url, options = {}) => {
    const handler = routes[String(url)];
    if (!handler) throw new Error(`unexpected request ${url}`);
    return handler(options);
  });
}

function open(fetcher) {
  return render(
    <LanguageProvider>
      <PackageChat fetcher={fetcher} />
    </LanguageProvider>
  );
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.setItem("agentlab.lang", "en");
});

describe("PackageChat", () => {
  it("asks an OIDC package's visitor to sign in with the company account first", async () => {
    open(runtime({ "/ui-config": () => json(OIDC_CONFIG) }));

    expect(await screen.findByRole("button", { name: /Sign in with your company account/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Message")).not.toBeInTheDocument();
  });

  it("says plainly when the package has no client to sign in as", async () => {
    open(runtime({ "/ui-config": () => json({ ...OIDC_CONFIG, auth: { mode: "oidc", oidc: { ...OIDC_CONFIG.auth.oidc, client_id: null } } }) }));

    expect(await screen.findByText(/auth\.oidc\.client_id/)).toBeInTheDocument();
  });

  it("signed in: asks, shows the sources and the mail verbatim, and approves it as the asker", async () => {
    sessionStorage.setItem("agent-package.token", TOKEN);
    const fetcher = runtime({
      "/ui-config": () => json(OIDC_CONFIG),
      "/chat": () => sse(...FIRST_LEG),
      "/approvals/42": () =>
        sse(
          ["node", { node_id: "approval", node_type: "human_approval", port_taken: "approved", content: "Người duyệt đã đồng ý" }],
          ["tool", { node_id: "send", tool: "gmail.send_email", args: REMINDER, simulated: false }],
          ["tool_result", { node_id: "send", tool: "gmail.send_email", ok: true, result: '{"message_id": "m-1", "sent": true}' }],
          ["node", { node_id: "report", node_type: "agent_llm", content: "Đã gửi mail nhắc cho khách C1." }]
        ),
    });
    const user = userEvent.setup();
    open(fetcher);

    expect(await screen.findByText("an@cty.vn")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "nhắc khach@example.com về hoá đơn C1");
    await user.click(screen.getByRole("button", { name: /Send/ }));

    const [, asked] = fetcher.mock.calls.find(([url]) => url === "/chat");
    expect(asked.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(JSON.parse(asked.body)).toEqual({ message: "nhắc khach@example.com về hoá đơn C1", conversation_id: null });

    const card = await screen.findByTestId("approval-payload");
    expect(card).toHaveTextContent(REMINDER.to);
    expect(card).toHaveTextContent(REMINDER.subject);
    expect(card).toHaveTextContent(REMINDER.body);
    await user.click(screen.getByRole("button", { name: /Nguồn tham khảo \(1\)/ }));
    expect(screen.getByText("customer_id=C1, days_overdue=45")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Approve/ }));

    expect(await screen.findByText("Đã gửi mail nhắc cho khách C1.")).toBeInTheDocument();
    const [, decision] = fetcher.mock.calls.find(([url]) => url === "/approvals/42");
    expect(decision.headers.Authorization).toBe(`Bearer ${TOKEN}`);
    expect(JSON.parse(decision.body)).toEqual({ approved: true });
  });

  it("a rejection carries the reason the runtime insists on", async () => {
    sessionStorage.setItem("agent-package.token", TOKEN);
    const fetcher = runtime({
      "/ui-config": () => json(OIDC_CONFIG),
      "/chat": () => sse(...FIRST_LEG),
      "/approvals/42": () => sse(["node", { node_id: "report", node_type: "agent_llm", content: "Không gửi." }]),
    });
    const user = userEvent.setup();
    open(fetcher);

    await user.type(await screen.findByLabelText("Message"), "nhắc khách");
    await user.click(screen.getByRole("button", { name: /Send/ }));
    await user.click(await screen.findByRole("button", { name: /Reject/ }));
    await user.type(screen.getByPlaceholderText(/Why not/), "Sai người nhận");
    await user.click(screen.getAllByRole("button", { name: /Reject/ }).at(-1));

    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === "/approvals/42")).toBe(true));
    const [, decision] = fetcher.mock.calls.find(([url]) => url === "/approvals/42");
    expect(JSON.parse(decision.body)).toEqual({ approved: false, reason: "Sai người nhận" });
  });

  it("a sign-in the runtime no longer accepts sends the person back to sign in", async () => {
    sessionStorage.setItem("agent-package.token", TOKEN);
    const fetcher = runtime({
      "/ui-config": () => json(OIDC_CONFIG),
      "/chat": () => json({ detail: "Chưa xác thực được người hỏi" }, 401),
    });
    const user = userEvent.setup();
    open(fetcher);

    await user.type(await screen.findByLabelText("Message"), "xin chào");
    await user.click(screen.getByRole("button", { name: /Send/ }));

    expect(await screen.findByRole("button", { name: /Sign in with your company account/ })).toBeInTheDocument();
    expect(screen.getByText(/sign-in has ended/)).toBeInTheDocument();
    expect(sessionStorage.getItem("agent-package.token")).toBeNull();
  });

  it("behind a trusted gateway there is nothing to sign in to, and no token is sent", async () => {
    const fetcher = runtime({
      "/ui-config": () => json({ agent: "Trợ lý", auth: { mode: "trusted_header" } }),
      "/chat": () => sse(["node", { node_id: "a", node_type: "agent_llm", content: "Chào bạn." }]),
    });
    const user = userEvent.setup();
    open(fetcher);

    await user.type(await screen.findByLabelText("Message"), "xin chào");
    await user.click(screen.getByRole("button", { name: /Send/ }));

    expect(await screen.findByText("Chào bạn.")).toBeInTheDocument();
    const [, asked] = fetcher.mock.calls.find(([url]) => url === "/chat");
    expect(asked.headers.Authorization).toBeUndefined();
    expect(screen.queryByRole("button", { name: /Sign out/ })).not.toBeInTheDocument();
  });

  it("a jwt package takes a pasted token", async () => {
    const fetcher = runtime({ "/ui-config": () => json({ agent: "Trợ lý", auth: { mode: "jwt" } }) });
    const user = userEvent.setup();
    open(fetcher);

    await user.type(await screen.findByLabelText(/Paste the access token/), TOKEN);
    await user.click(screen.getByRole("button", { name: /Continue/ }));

    expect(await screen.findByLabelText("Message")).toBeInTheDocument();
    expect(sessionStorage.getItem("agent-package.token")).toBe(TOKEN);
  });
});
