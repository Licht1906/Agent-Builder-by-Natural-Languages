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

/* DS-05: "Chạy với tư cách…" — the creator stands in for one of their
   company's employees, so a data_query reads that person's rows. */

const draw = (readsDatabase = true) =>
  render(
    <LanguageProvider>
      <SandboxTab agentId={1} versionId={2} readsDatabase={readsDatabase} onActiveNode={vi.fn()} onVersionChanged={vi.fn()} notify={vi.fn()} />
    </LanguageProvider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  api.workflow.pendingApprovals.mockResolvedValue([]);
  api.runs.sandbox.mockResolvedValue(undefined);
  api.runs.askers.mockResolvedValue([
    { value: "an@cty.vn", position: "Nhân viên KD", source: "ERP staging" },
    { value: "binh@cty.vn", position: "Trưởng nhóm KD", source: "ERP staging" },
  ]);
});

describe("the sandbox's run-as box", () => {
  it("is not there for a graph that reads no database", async () => {
    draw(false);

    await waitFor(() => expect(api.workflow.pendingApprovals).toHaveBeenCalled());
    expect(screen.queryByLabelText(/Run as/)).toBeNull();
    expect(api.runs.askers).not.toHaveBeenCalled();
  });

  it("suggests the employee view's people, with their position", async () => {
    draw();

    await waitFor(() => expect(api.runs.askers).toHaveBeenCalledWith(1, 2, ""));
    const options = [...document.querySelectorAll("datalist option")].map((option) => [option.value, option.label]);
    expect(options).toEqual([
      ["an@cty.vn", "Nhân viên KD"],
      ["binh@cty.vn", "Trưởng nhóm KD"],
    ]);
  });

  it("sends whoever is picked with the run, and says who the run was for", async () => {
    const actor = userEvent.setup();
    draw();

    await actor.type(screen.getByLabelText(/Run as/), "an@cty.vn");
    await actor.type(screen.getByLabelText(/Test message/), "khách nào quá hạn?");
    await actor.click(screen.getByRole("button", { name: /Run in sandbox/ }));

    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalled());
    const [, , message, , options] = api.runs.sandbox.mock.calls[0];
    expect(message).toBe("khách nào quá hạn?");
    expect(options).toMatchObject({ runAs: "an@cty.vn" });
    expect(await screen.findByText(/Ran as an@cty.vn/)).toBeInTheDocument();
  });

  it("takes anyone typed, not only a suggestion", async () => {
    const actor = userEvent.setup();
    draw();

    await actor.type(screen.getByLabelText(/Run as/), "nguoi.la@cty.vn");
    await actor.type(screen.getByLabelText(/Test message/), "xem hết");
    await actor.click(screen.getByRole("button", { name: /Run in sandbox/ }));

    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalled());
    expect(api.runs.sandbox.mock.calls[0][4]).toMatchObject({ runAs: "nguoi.la@cty.vn" });
  });
});

describe("a trial conversation", () => {
  it("keeps one thread across turns, so the asker can change mid-conversation", async () => {
    const actor = userEvent.setup();
    api.runs.sandbox.mockImplementation(async (_agent, _version, _message, onEvent) => {
      onEvent("thread", { conversation_id: "trial-1" });
    });
    draw();

    await actor.type(screen.getByLabelText(/Run as/), "an@cty.vn");
    await actor.type(screen.getByLabelText(/Test message/), "lượt một");
    await actor.click(screen.getByRole("button", { name: /Run in sandbox/ }));
    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalledTimes(1));

    await actor.clear(screen.getByLabelText(/Run as/));
    await actor.type(screen.getByLabelText(/Run as/), "binh@cty.vn");
    await actor.type(screen.getByLabelText(/Test message/), "lượt hai");
    await actor.click(screen.getByRole("button", { name: /Run in sandbox/ }));
    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalledTimes(2));

    expect(api.runs.sandbox.mock.calls[0][4]).toMatchObject({ runAs: "an@cty.vn", conversationId: null });
    expect(api.runs.sandbox.mock.calls[1][4]).toMatchObject({ runAs: "binh@cty.vn", conversationId: "trial-1" });
  });

  it("starts a fresh thread on request", async () => {
    const actor = userEvent.setup();
    api.runs.sandbox.mockImplementation(async (_agent, _version, _message, onEvent) => {
      onEvent("thread", { conversation_id: "trial-1" });
    });
    draw();
    await actor.type(screen.getByLabelText(/Test message/), "lượt một");
    await actor.click(screen.getByRole("button", { name: /Run in sandbox/ }));
    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalledTimes(1));

    await actor.click(screen.getByRole("button", { name: /New conversation/ }));
    await actor.type(screen.getByLabelText(/Test message/), "lại từ đầu");
    await actor.click(screen.getByRole("button", { name: /Run in sandbox/ }));

    await waitFor(() => expect(api.runs.sandbox).toHaveBeenCalledTimes(2));
    expect(api.runs.sandbox.mock.calls[1][4]).toMatchObject({ conversationId: null });
  });
});
