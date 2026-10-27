import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import SandboxTab from "./SandboxTab";
import { LanguageProvider } from "../../i18n";
import * as api from "../../lib/api";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual("../../lib/api");
  return {
    ...actual,
    workflow: { pendingApprovals: vi.fn() },
    runs: { sandbox: vi.fn(), askers: vi.fn(), decideApproval: vi.fn() },
  };
});

/* DS-09: "Gửi thật" — for one run, the actions a person approves go out for
   real. Off by default, and never remembered into the next run. */

const draw = () =>
  render(
    <LanguageProvider>
      <SandboxTab agentId={1} versionId={2} onActiveNode={vi.fn()} onVersionChanged={vi.fn()} notify={vi.fn()} />
    </LanguageProvider>,
  );

const sendForReal = () => screen.getByRole("checkbox", { name: /Send for real|Gửi thật/ });

async function runOnce(actor) {
  await actor.clear(screen.getByLabelText(/Test message|Tin nhắn thử/));
  await actor.type(screen.getByLabelText(/Test message|Tin nhắn thử/), "gửi thư cho khách");
  await actor.click(screen.getByRole("button", { name: /Run in sandbox|Chạy/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  api.workflow.pendingApprovals.mockResolvedValue([]);
  api.runs.sandbox.mockResolvedValue(undefined);
});

describe("the sandbox's Send for real switch", () => {
  it("is off by default, and a run sent without touching it is a rehearsal", async () => {
    const actor = userEvent.setup();
    draw();
    await waitFor(() => expect(api.workflow.pendingApprovals).toHaveBeenCalled());

    expect(sendForReal()).not.toBeChecked();
    await runOnce(actor);

    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalledTimes(1));
    expect(api.runs.sandbox.mock.calls[0][4]).toMatchObject({ sendForReal: false });
  });

  it("goes with the one run it was switched on for, then is off again", async () => {
    const actor = userEvent.setup();
    draw();
    await waitFor(() => expect(api.workflow.pendingApprovals).toHaveBeenCalled());

    await actor.click(sendForReal());
    await runOnce(actor);
    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalledTimes(1));
    expect(api.runs.sandbox.mock.calls[0][4]).toMatchObject({ sendForReal: true });
    await waitFor(() => expect(sendForReal()).not.toBeChecked());

    await runOnce(actor);
    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalledTimes(2));
    expect(api.runs.sandbox.mock.calls[1][4]).toMatchObject({ sendForReal: false });
  });

  it("warns on the approval card that approving sends for real", async () => {
    api.workflow.pendingApprovals.mockResolvedValue([
      { approval_id: 7, node_id: "approval", approver_role: "manager", message: "Duyệt?", actions: [], live_sends: true },
    ]);
    draw();

    expect(await screen.findByRole("alert")).toHaveTextContent(/for real|gửi thật/i);
  });

  it("says nothing of the kind on a rehearsal's card", async () => {
    api.workflow.pendingApprovals.mockResolvedValue([
      { approval_id: 7, node_id: "approval", approver_role: "manager", message: "Duyệt?", actions: [], live_sends: false },
    ]);
    draw();

    await screen.findByText("Duyệt?");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
