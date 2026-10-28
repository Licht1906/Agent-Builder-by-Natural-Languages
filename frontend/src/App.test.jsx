import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "./App";
import { LanguageProvider } from "./i18n";
import * as api from "./lib/api";

vi.mock("./lib/api", async () => {
  const actual = await vi.importActual("./lib/api");
  return {
    ...actual,
    getToken: vi.fn(() => "a-token"),
    clearToken: vi.fn(),
    auth: { me: vi.fn() },
    approvals: { queue: vi.fn().mockResolvedValue([]) },
    agents: { mine: vi.fn().mockResolvedValue([]), running: vi.fn().mockResolvedValue([]) },
    adminUsers: { list: vi.fn().mockResolvedValue([]), activate: vi.fn() },
    knowledgeBases: { list: vi.fn().mockResolvedValue([]) },
  };
});

const draw = () => render(<LanguageProvider><App /></LanguageProvider>);

beforeEach(() => {
  vi.clearAllMocks();
  // The sidebar's collapsed/expanded choice is remembered in localStorage;
  // clear it so one test's toggle does not leak into the next.
  localStorage.clear();
  api.getToken.mockReturnValue("a-token");
  api.auth.me.mockResolvedValue({ id: 1, email: "agent-admin@example.com", role: "agent_admin" });
  api.approvals.queue.mockResolvedValue([]);
  api.agents.mine.mockResolvedValue([]);
  api.agents.running.mockResolvedValue([]);
});

/* The shell is `height:100vh; overflow:hidden` and owns no scroll of its own —
   that lives in the page area inside it. So a page rendered as the shell's
   *sibling* rather than its child starts one full viewport below the fold:
   present in the DOM, blank to whoever clicked the tab. Every Enterprise
   screen, Knowledge Bases and Accounts were all built that way once.

   Asserting on the DOM relationship rather than counting headers, because the
   header count only ever caught the version of this bug where the stray page
   drew a second one — and no page draws its own header any more. */
const shell = () => document.querySelector(".ab-shell");

/** The page area's content, and whether it is where a page can actually be seen. */
const pageIsInsideShell = () => {
  const page = document.querySelector(".ab-page");
  expect(page).not.toBeNull();
  return shell().contains(page);
};


describe("the Data sources tab", () => {
  /* Data sources (formerly Knowledge Bases) used to live inside the Enterprise
     disclosure; it now sits in the Workspace section directly under the
     Workflow builder, reachable in one click with no disclosure to open. It
     was also one of the last screens still drawing its own header, so it is
     among the likeliest to be put back outside the shell by a later merge. */
  it("puts the page inside the shell, not one viewport below the fold", async () => {
    const actor = userEvent.setup();
    draw();
    await waitFor(() => expect(screen.getByText("Agentlab")).toBeInTheDocument());

    await actor.click(screen.getByRole("button", { name: /Data sources|Nguồn dữ liệu/i }));

    await waitFor(() => expect(api.knowledgeBases.list).toHaveBeenCalled());
    expect(pageIsInsideShell()).toBe(true);
  });
});

/* The Enterprise (UC-1) feature area went with the UC-1 pipeline (DS-21):
   no route, no nav entry, nothing here to click. */
describe("the Enterprise feature area is gone from the nav", () => {
  it("shows no Enterprise section or review queue", async () => {
    draw();
    await waitFor(() => expect(screen.getByText("Agentlab")).toBeInTheDocument());

    expect(screen.queryByRole("button", { name: /^Enterprise$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Enterprise workbench|Xưởng Enterprise/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Enterprise review queue|Hàng chờ duyệt Enterprise/i })).toBeNull();
  });
});

describe("the Admin section for an admin-equivalent role", () => {
  it("reaches Accounts in the shell", async () => {
    const actor = userEvent.setup();
    draw();
    await waitFor(() => expect(screen.getByText("Agentlab")).toBeInTheDocument());

    expect(screen.getByRole("button", { name: /Accounts|Tài khoản/i })).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: /Accounts|Tài khoản/i }));

    await waitFor(() => expect(api.adminUsers.list).toHaveBeenCalled());
    expect(pageIsInsideShell()).toBe(true);
  });
});

describe("the signed-in session", () => {
  it("lands on the Designer's home stage, not the agent list", async () => {
    draw();
    await waitFor(() => expect(screen.getByText("Agentlab")).toBeInTheDocument());

    expect(screen.getByRole("heading", { name: /What are we building today|Hôm nay/i })).toBeInTheDocument();
  });
});
