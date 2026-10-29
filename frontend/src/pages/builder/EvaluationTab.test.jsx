import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import EvaluationTab from "./EvaluationTab";
import { LanguageProvider } from "../../i18n";
import * as api from "../../lib/api";

/* What a creator could be misled by is what gets pinned.
 *
 * ADR-0056 split this screen into two facets because the suite was measured
 * two ways and the two numbers must never read as one. ADR-0065 retired the
 * second way, so the thing to pin is the opposite: **one set, one score, one
 * list**, with no control on screen that promises a measurement nothing can
 * produce. The ten tests that drove the grid facet went with it.
 *
 * ADR-0056 §8's rule survives the merge and is still pinned: the figures the
 * headline replaced must stay in the accessibility tree on one hover, and the
 * gate's refusal must still name the score, the bar, the distance and the next
 * move even while it is showing one line of itself.
 *
 * The rest is unchanged in substance: the suite must say which cases no longer
 * apply to this version (orphan / ungrounded) instead of silently shrinking;
 * the upload must show its per-row report BEFORE anything is written; and the
 * run menu must send exactly the ids the creator picked. */

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual("../../lib/api");
  return {
    ...actual,
    evaluation: {
      generate: vi.fn(),
      cases: vi.fn(),
      run: vi.fn(),
      latest: vi.fn(),
      runById: vi.fn(),
      updateCase: vi.fn(),
      removeCase: vi.fn(),
      importSuite: vi.fn(),
      exportSuite: vi.fn(),
      template: vi.fn(),
      sample: vi.fn().mockResolvedValue({ available: false, name: null, case_count: 0 }),
      loadSample: vi.fn(),
    },
  };
});

const CASES = [
  {
    id: 1, criterion: "Trả lời đúng chính sách", input_message: "hoàn tiền mất bao lâu", expected: "nói 7 ngày",
    origin: "generated", category: "correctness", expected_check: null, tags: [], cell_key: null, zone: null,
    created_at: "2026-08-23T00:00:00", updated_at: "2026-08-23T00:00:00",
  },
  {
    id: 2, criterion: "Không gọi action đã bỏ", input_message: "gửi thư", expected: "không gọi send_email",
    origin: "uploaded", category: "tool_correctness", expected_check: { kind: "forbids_action", action: "gmail.send_email" },
    tags: ["nạp-đợt-1"], cell_key: null, zone: null,
    created_at: "2026-08-23T00:00:00", updated_at: "2026-08-23T00:00:00",
  },
  /* Two rows the retired grid left in this agent's suite (ADR-0065). They are
     ordinary cases now — listed, filterable, runnable, deletable — and the
     screen must not pretend they belong to a regime it no longer has. */
  {
    id: 3, criterion: "Ô lưới: input_shape=standard · approval:duyet=approved", input_message: "Hãy giúp tôi: xử lý đơn.",
    expected: "chạy tới End", origin: "grid", category: "workflow_success", expected_check: { kind: "reaches_end" },
    tags: [], cell_key: "input_shape=standard|approval:duyet=approved", zone: "am",
    created_at: "2026-08-23T00:00:00", updated_at: "2026-08-23T00:00:00",
  },
  {
    id: 4, criterion: "Ô lưới: input_shape=missing_information · approval:duyet=rejected", input_message: "Giúp tôi việc này nhé.",
    expected: "không gửi khi bị từ chối", origin: "grid", category: "human_approval_compliance", expected_check: { kind: "approval_compliance" },
    tags: [], cell_key: "input_shape=missing_information|approval:duyet=rejected", zone: "nong",
    created_at: "2026-08-23T00:00:00", updated_at: "2026-08-23T00:00:00",
  },
];

const LATEST = {
  id: 9, version_id: 13, passed_count: 1, total_count: 2, pass_rate: 0.5, threshold: 0.6, threshold_at_run: 0.6,
  meets_threshold: false, gap: 0.1, cases_short: 1, blocked_reason: null, judge_model: "gpt-4.1", created_at: "2026-08-23T00:00:00",
  results: [
    { case_id: 1, suite_case_id: 1, applicability: "orphan", criterion: "Trả lời đúng chính sách", input_message: "hoàn tiền mất bao lâu", expected: "nói 7 ngày", answer: "7 ngày", passed: true, reason: "đúng", category: "correctness", expected_check: null, safety_violation: false, metrics: [], evidence: [] },
  ],
  skipped: [{ suite_case_id: 2, reason: "gmail.send_email không có trong graph của bản này" }],
};

const draw = (notify = vi.fn()) => {
  render(<LanguageProvider><EvaluationTab agentId={7} versionId={13} notify={notify} /></LanguageProvider>);
  return notify;
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem("agentlab.lang", "en");
  api.evaluation.cases.mockResolvedValue(CASES);
  api.evaluation.latest.mockResolvedValue(LATEST);
});

describe("EvaluationTab", () => {
  it("shows one score against one bar, with no second regime anywhere on the screen", async () => {
    draw();

    /* One headline, and the figures ADR-0056 §8 folded away are still in the
       accessibility tree on one hover: the bar in force, how far short, and
       how many could not be run at all. */
    const meter = await screen.findByRole("img");
    expect(meter).toHaveAttribute("title", expect.stringMatching(/1\/2 passed/));
    expect(meter).toHaveAttribute("title", expect.stringMatching(/bar 60%/));
    expect(meter).toHaveAttribute("title", expect.stringMatching(/1 case\(s\) short/));
    expect(meter).toHaveAttribute("title", expect.stringMatching(/skipped \(ungrounded\)/i));

    // Nothing offers a scenario grid: no facet switch, no build door, no run
    // door for a measurement this platform no longer performs (ADR-0065).
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /Build grid/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run the grid/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Name the axes for me/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Scenario grid/)).not.toBeInTheDocument();
  });

  it("folds the gate's refusal without dropping the score, the bar, the distance or the next move", async () => {
    const reason =
      "Eval gần nhất đạt 1/2 case (tỷ lệ 0.50), dưới ngưỡng 0.60 — còn thiếu 1 case nữa. " +
      "Việc tiếp theo: đọc lý do không đạt của từng case, sửa spec nếu agent thật sự sai.";
    api.evaluation.latest.mockResolvedValue({ ...LATEST, blocked_reason: reason });
    draw();
    // One line on screen, the whole refusal in the title — ADR-0011 §5 asks
    // for four things and folding may not cost any of them.
    const note = await screen.findByTitle(reason);
    expect(note).toBeInTheDocument();
    expect(note).toHaveTextContent(/1\/2 case/);
    expect(note).toHaveTextContent(/ngưỡng 0.60/);
    expect(note).toHaveTextContent(/thiếu 1 case/);
    expect(note).toHaveTextContent(/Việc tiếp theo/);
  });

  it("says which cases no longer apply to this version instead of hiding them", async () => {
    draw();
    expect(await screen.findByText(/criterion no longer in this version/)).toBeInTheDocument();
    expect(screen.getByText(/not run — names something this graph does not have/)).toBeInTheDocument();
    // The two status chips carry their own counts, so a hole is visible before
    // anything is filtered.
    expect(screen.getByRole("button", { name: /criterion gone 1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^ungrounded 1/ })).toBeInTheDocument();
    // …and no chip for a bucket that is empty: nothing failed in this run, so
    // a "failing" filter would only ever produce an empty list.
    expect(screen.queryByRole("button", { name: /^failing/ })).not.toBeInTheDocument();
  });

  it("lists every provenance in one list, the retired grid's leftovers included", async () => {
    draw();
    // Provenance is a filter over one list, and after ADR-0065 that list holds
    // everything the suite holds — a row a retired feature wrote is still a
    // row this creator owns and can delete.
    expect(await screen.findByRole("button", { name: /machine-written 1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /uploaded 1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /from the old grid 2/ })).toBeInTheDocument();
    expect(screen.getByText("Ô lưới: input_shape=standard · approval:duyet=approved")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /machine-written 1/ }));
    expect(screen.getByText("Trả lời đúng chính sách")).toBeInTheDocument();
    expect(screen.queryByText("Không gọi action đã bỏ")).not.toBeInTheDocument();
    expect(screen.queryByText("Ô lưới: input_shape=standard · approval:duyet=approved")).not.toBeInTheDocument();
  });

  it("keeps a failed case open and folds a passing one", async () => {
    draw();
    // Case 1 passed here: its run and the judge's reason are folded, not
    // deleted — still in the DOM, still reachable in one click.
    const summary = await screen.findByText(/what it did and answered/);
    const fold = summary.closest("details");
    expect(fold).not.toHaveAttribute("open");
    expect(within(fold).getByText(/Verdict:/)).toBeInTheDocument();
    await userEvent.click(summary);
    expect(fold).toHaveAttribute("open");
  });

  it("shows what the agent did and said, not only the verdict", async () => {
    const withRun = {
      ...LATEST,
      results: [{
        ...LATEST.results[0],
        answer: "Đã gửi thư mời tới ba người được mời.",
        passed: false,
        reason: "nói đã gửi trong khi tool hỏng",
        evidence: [
          { tool: "gmail.send_email", args: { to: "a@b.c" }, ok: false, result: "HTTP 502", simulated: false },
          { tool: "google_drive.search", args: {}, ok: true, result: "2 tài liệu", simulated: true },
        ],
      }],
    };
    api.evaluation.latest.mockResolvedValue(withRun);
    draw();

    // What it did, in order, with each call's own verdict…
    expect(await screen.findByText("Did:")).toBeInTheDocument();
    expect(screen.getByText("gmail.send_email ✗")).toBeInTheDocument();
    expect(screen.getByText(/google_drive.search ✓ ~/)).toBeInTheDocument();
    // …what it answered…
    expect(screen.getByText(/Đã gửi thư mời tới ba người được mời/)).toBeInTheDocument();
    // …and the raw record still one fold further down.
    expect(screen.getByText(/Evidence: 2 tool call/)).toBeInTheDocument();
    // A failing case is never folded: it is the case d81dfe4 opened up.
    expect(screen.queryByText(/what it did and answered/)).not.toBeInTheDocument();
  });

  /* EV-01: the POST answers `running`; the screen polls the run, shows how far
     along it is, and says so once it has finished. */
  it("shows a background run's progress and announces it when it finishes", async () => {
    api.evaluation.run.mockResolvedValue({ ...LATEST, id: 10, status: "running", done_count: 0, total_count: 150, results: [] });
    api.evaluation.runById
      .mockResolvedValueOnce({ ...LATEST, id: 10, status: "running", done_count: 37, total_count: 150, results: [] })
      .mockResolvedValue({ ...LATEST, id: 10, status: "completed", done_count: 2, passed_count: 2, total_count: 2 });
    const notify = draw();
    await userEvent.click(await screen.findByRole("button", { name: /Run evaluation/ }));
    await userEvent.click(screen.getByRole("button", { name: /All cases/ }));

    expect(await screen.findByText("Running 0/150")).toBeInTheDocument();
    expect(await screen.findByText("Running 37/150", {}, { timeout: 3500 })).toBeInTheDocument();
    await waitFor(() => expect(notify).toHaveBeenCalledWith("2/2 checks passed"), { timeout: 3500 });
    expect(api.evaluation.runById).toHaveBeenCalledWith(7, 13, 10);
  }, 10000);

  it("runs only the failed or the selected cases with exactly those ids", async () => {
    const failed = {
      ...LATEST,
      results: [{ ...LATEST.results[0], passed: false, reason: "sai" }],
    };
    api.evaluation.latest.mockResolvedValue(failed);
    api.evaluation.run.mockResolvedValue({ ...LATEST, passed_count: 1 });
    draw();
    await userEvent.click(await screen.findByRole("button", { name: /Run evaluation/ }));
    await userEvent.click(screen.getByRole("button", { name: /Only the failed ones/ }));
    await waitFor(() => expect(api.evaluation.run).toHaveBeenCalledWith(7, 13, [1]));

    await userEvent.click((await screen.findAllByRole("checkbox", { name: /Select this case/ }))[1]);
    await userEvent.click(screen.getByRole("button", { name: /Run evaluation/ }));
    await userEvent.click(screen.getByRole("button", { name: /Selected \(1\)/ }));
    await waitFor(() => expect(api.evaluation.run).toHaveBeenLastCalledWith(7, 13, [2]));
  });

  it("previews an upload row by row and writes nothing until the creator confirms", async () => {
    const report = {
      dry_run: true, added_count: 2, skipped_count: 1, error_count: 1,
      rows: [
        { row: 1, status: "added", reason: null },
        { row: 2, status: "added", reason: null },
        { row: 3, status: "skipped", reason: "trùng (input, criterion)" },
        { row: 4, status: "error", reason: "thiếu expected" },
      ],
    };
    api.evaluation.importSuite.mockResolvedValueOnce(report);
    const notify = draw();
    await userEvent.click(await screen.findByRole("button", { name: /Upload cases/ }));
    const dialog = screen.getByRole("dialog");
    const file = new File(["input,expected\na,b"], "bo-de.csv", { type: "text/csv" });
    await userEvent.upload(within(dialog).getByLabelText(/Choose a file/), file);

    expect(await within(dialog).findByText(/2 will be added/)).toBeInTheDocument();
    expect(within(dialog).getByText("thiếu expected")).toBeInTheDocument();
    expect(api.evaluation.importSuite).toHaveBeenCalledWith(7, file, true);

    api.evaluation.importSuite.mockResolvedValueOnce({ ...report, dry_run: false });
    await userEvent.click(within(dialog).getByRole("button", { name: /Import 2 valid row/ }));
    await waitFor(() => expect(api.evaluation.importSuite).toHaveBeenLastCalledWith(7, file, false));
    expect(notify).toHaveBeenCalledWith("2 case(s) imported, 1 skipped, 1 error(s)");
  });

  it("deletes a leftover grid row like any other case", async () => {
    api.evaluation.removeCase.mockResolvedValue(undefined);
    draw();
    const row = (await screen.findByText("Ô lưới: input_shape=standard · approval:duyet=approved")).closest(".ab-eval-item");
    await userEvent.click(within(row).getByRole("button", { name: /Delete this test case/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Delete test case/ }));
    await waitFor(() => expect(api.evaluation.removeCase).toHaveBeenCalledWith(7, 13, 3));
  });
});
