import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "../i18n";
import { AdminDossier, AdminQueue } from "./AdminApprovals";
import * as api from "../lib/api";

/* The half of the review screen ADR-0064 adds: what happens to a version after
 * somebody decides about it.
 *
 * Two things are worth testing here and neither is visible from the API suite.
 * First, that the settled list is a *different fetch* — a tab that meant to show
 * decided reviews and called `queue()` would render an empty page and look like
 * a backend problem. Second, that a settled dossier offers no way to decide:
 * approve and reject refuse anything but `pending_approval`, so a button here
 * would be a button that 409s, which is the green-light-over-a-refusing-button
 * shape ADR-0011 §5 exists to keep out. */

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    approvals: { queue: vi.fn(), decided: vi.fn(), dossier: vi.fn(), approve: vi.fn(), reject: vi.fn(), overrideEval: vi.fn() },
  };
});

const decidedRow = (overrides) => ({
  version_id: 133,
  agent_id: 108,
  agent_name: "Hỏi đáp sổ tay",
  owner_email: "creator@example.com",
  status: "approved",
  decided_by: "admin@example.com",
  decided_at: "2026-08-26T12:24:42.147952",
  note: null,
  eval_override_reason: null,
  score: { passed_count: 3, total_count: 3, pass_rate: 1, threshold: 0.6, meets_threshold: true },
  created_at: "2026-08-26T10:57:34.991252",
  ...overrides,
});

const dossier = (overrides) => ({
  version_id: 133,
  agent_id: 108,
  agent_name: "Hỏi đáp sổ tay",
  owner_email: "creator@example.com",
  status: "approved",
  created_at: "2026-08-26T10:57:34.991252",
  spec: null,
  flow: null,
  tools: [],
  allowed_domains: [],
  solution_spec: { goal: "Trả lời câu hỏi về sổ tay nhân viên", actions: [], prohibitions: [] },
  workflow: null,
  actions: [],
  budget: null,
  sandbox_run: { id: 1, status: "succeeded", transcript: [] },
  eval_run: { id: 1, passed_count: 3, total_count: 3 },
  eval_breakdown: null,
  blocked_reason: null,
  costs: { version_id: 133, estimate: null, worst_case: null, measured: null },
  rejection_reason: null,
  decisions: [],
  ...overrides,
});

const drawQueue = (tab = "pending", { openVersion = vi.fn(), setTab = vi.fn() } = {}) => {
  render(<LanguageProvider><AdminQueue openVersion={openVersion} tab={tab} setTab={setTab} /></LanguageProvider>);
  return { openVersion, setTab };
};

const drawDossier = ({ back = vi.fn(), notify = vi.fn() } = {}) => {
  render(<LanguageProvider><AdminDossier versionId={133} back={back} notify={notify} /></LanguageProvider>);
  return { back, notify };
};

beforeEach(() => {
  vi.clearAllMocks();
  api.approvals.queue.mockResolvedValue([]);
  api.approvals.decided.mockResolvedValue([]);
});

describe("AdminQueue", () => {
  it("opens on the pending queue and does not fetch the decision history to draw it", async () => {
    // The default must stay the queue and must not get slower: a screen that
    // loaded the whole decided history to render "nothing is waiting on you"
    // would pay for the platform's entire past on every visit.
    drawQueue();

    await waitFor(() => expect(api.approvals.queue).toHaveBeenCalled());
    expect(api.approvals.decided).not.toHaveBeenCalled();
    expect(await screen.findByText(/Nothing is waiting on you/)).toBeInTheDocument();
  });

  it("asks for the decided list — not the queue — when the settled tab is showing", async () => {
    api.approvals.decided.mockResolvedValue([decidedRow()]);

    drawQueue("decided");

    await waitFor(() => expect(api.approvals.decided).toHaveBeenCalled());
    expect(api.approvals.queue).not.toHaveBeenCalled();
  });

  it("names who decided and when, and offers the dossier read-only", async () => {
    const actor = userEvent.setup();
    api.approvals.decided.mockResolvedValue([decidedRow()]);
    const { openVersion } = drawQueue("decided");

    expect(await screen.findByText(/Hỏi đáp sổ tay/)).toBeInTheDocument();
    expect(screen.getByText(/Decided by admin@example\.com/)).toBeInTheDocument();

    await actor.click(screen.getByRole("button", { name: /Open dossier/ }));
    expect(openVersion).toHaveBeenCalledWith(133);
  });

  it("says outright when nobody recorded the decision, rather than showing a date", async () => {
    // Two of this platform's three approved versions are in exactly this state.
    // A row that filled the blank with `created_at` would present the day the
    // version was drafted as the day somebody approved it — and a made-up
    // timestamp is indistinguishable from a real one.
    api.approvals.decided.mockResolvedValue([decidedRow({ decided_by: null, decided_at: null, score: null })]);

    drawQueue("decided");

    expect(await screen.findByText(/No decision record/)).toBeInTheDocument();
    expect(screen.queryByText(/Decided by/)).not.toBeInTheDocument();
  });

  it("shows the justification a waived eval bar was granted on", async () => {
    api.approvals.decided.mockResolvedValue([decidedRow({ eval_override_reason: "Judge chấm sai case 2" })]);

    drawQueue("decided");

    expect(await screen.findByText(/Judge chấm sai case 2/)).toBeInTheDocument();
  });

  it("switches lists through the tab rather than remembering its own", async () => {
    const actor = userEvent.setup();
    const { setTab } = drawQueue("pending");
    await waitFor(() => expect(api.approvals.queue).toHaveBeenCalled());

    await actor.click(screen.getByRole("button", { name: /Decided/ }));

    expect(setTab).toHaveBeenCalledWith("decided");
  });
});

describe("AdminDossier", () => {
  it("offers no way to decide a review that is already over", async () => {
    api.approvals.dossier.mockResolvedValue(dossier({ decisions: [
      { id: 56, kind: "approved", reason: null, admin_email: "admin@example.com", eval_score: null, created_at: "2026-08-26T12:24:42.147952" },
    ] }));

    drawDossier();

    expect(await screen.findByText(/The decision/)).toBeInTheDocument();
    expect(screen.getByText(/read-only/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request changes/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Waive the eval bar/ })).not.toBeInTheDocument();
    // The audit trail is the reason to be able to open this at all.
    expect(screen.getByText("admin@example.com", { exact: false })).toBeInTheDocument();
  });

  it("still shows the whole evidence pack behind a decided version", async () => {
    api.approvals.dossier.mockResolvedValue(dossier());

    drawDossier();

    expect(await screen.findByText("succeeded")).toBeInTheDocument();
    expect(screen.getByText("3/3")).toBeInTheDocument();
    expect(screen.getByText(/Trả lời câu hỏi về sổ tay nhân viên/)).toBeInTheDocument();
  });

  it("says so when an approved version has no decision record behind it", async () => {
    api.approvals.dossier.mockResolvedValue(dossier({ decisions: [] }));

    drawDossier();

    expect(await screen.findByText(/No decision record/)).toBeInTheDocument();
  });

  it("still offers the decision on a version that is genuinely waiting for one", async () => {
    // The guard against over-reading the rule: the read-only view must key off
    // `status`, not off "this dossier has decisions on it" — a version that was
    // overridden into the queue has a decision row and is still awaiting one.
    api.approvals.dossier.mockResolvedValue(dossier({
      status: "pending_approval",
      decisions: [
        { id: 41, kind: "eval_override", reason: "Judge chấm sai case 2", admin_email: "admin@example.com", eval_score: null, created_at: "2026-08-26T11:00:00" },
      ],
    }));

    drawDossier();

    expect(await screen.findByRole("button", { name: /Approve/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Request changes/ })).toBeInTheDocument();
    expect(screen.queryByText(/read-only/)).not.toBeInTheDocument();
  });
});
