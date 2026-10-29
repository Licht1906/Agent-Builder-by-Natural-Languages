import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import EvaluationTab from "./EvaluationTab";
import { LanguageProvider } from "../../i18n";
import * as api from "../../lib/api";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual("../../lib/api");
  return {
    ...actual,
    evaluation: {
      generate: vi.fn(), cases: vi.fn(), run: vi.fn(), latest: vi.fn(), updateCase: vi.fn(),
      removeCase: vi.fn(), importSuite: vi.fn(), exportSuite: vi.fn(), template: vi.fn(),
      sample: vi.fn().mockResolvedValue({ available: false, name: null, case_count: 0 }), loadSample: vi.fn(),
      recordCassettes: vi.fn(), runById: vi.fn(),
    },
  };
});

/* DS-05: an eval case is a question asked by somebody — "nhân viên KD hỏi
   xem hết" — and the case says who. */

const CASE = {
  id: 7, criterion: "Nhân viên KD chỉ thấy khách của mình", input_message: "xem hết khách quá hạn", expected: "chỉ khách của mình",
  origin: "manual", category: "workflow_success", expected_check: { kind: "reaches_end" }, tags: [], run_as: null,
  cell_key: null, zone: null, created_at: "2026-10-26T00:00:00", updated_at: "2026-10-26T00:00:00",
};

const draw = (readsDatabase = true) =>
  render(<LanguageProvider><EvaluationTab agentId={1} versionId={2} readsDatabase={readsDatabase} notify={vi.fn()} /></LanguageProvider>);

const rowOf = async (criterion) => (await screen.findByText(criterion)).closest(".ab-eval-item");

beforeEach(() => {
  vi.clearAllMocks();
  api.evaluation.latest.mockRejectedValue(Object.assign(new Error("none"), { status: 404 }));
});

describe("frozen data for eval (EV-02)", () => {
  it("re-records the suite's frozen data on a graph that reads a database, and says how it went", async () => {
    api.evaluation.cases.mockResolvedValue([{ ...CASE, run_as: "an@cty.vn" }]);
    api.evaluation.recordCassettes.mockResolvedValue({ recorded: 1, problems: [] });
    const notify = vi.fn();
    render(<LanguageProvider><EvaluationTab agentId={1} versionId={2} readsDatabase notify={notify} /></LanguageProvider>);

    await userEvent.click(await screen.findByRole("button", { name: /Re-record frozen data/ }));

    await waitFor(() => expect(api.evaluation.recordCassettes).toHaveBeenCalledWith(1, 2));
    expect(notify).toHaveBeenCalledWith("Frozen data re-recorded for 1 case(s)");
  });

  it("offers nothing to re-record on a graph that reads no database", async () => {
    api.evaluation.cases.mockResolvedValue([CASE]);
    draw(false);
    await screen.findByText(CASE.criterion);

    expect(screen.queryByRole("button", { name: /Re-record frozen data/ })).not.toBeInTheDocument();
  });
});

describe("an eval case's asker", () => {
  it("shows who a case asks as", async () => {
    api.evaluation.cases.mockResolvedValue([{ ...CASE, run_as: "an@cty.vn" }]);
    draw();

    const row = await rowOf(CASE.criterion);
    expect(within(row).getByText(/as an@cty.vn/)).toBeInTheDocument();
  });

  it("sets who a case asks as, on a graph that reads a database", async () => {
    const actor = userEvent.setup();
    api.evaluation.cases.mockResolvedValue([CASE]);
    api.evaluation.updateCase.mockResolvedValue({ ...CASE, run_as: "an@cty.vn" });
    draw();
    const row = await rowOf(CASE.criterion);

    await actor.click(within(row).getByRole("button", { name: /Run as…/ }));
    await actor.type(within(row).getByLabelText(/Run as/), "an@cty.vn{Enter}");

    await waitFor(() => expect(api.evaluation.updateCase).toHaveBeenCalledWith(1, 2, 7, { run_as: "an@cty.vn" }));
    expect(await within(row).findByText(/as an@cty.vn/)).toBeInTheDocument();
  });

  it("offers no asker on a graph that reads no database", async () => {
    api.evaluation.cases.mockResolvedValue([CASE]);
    draw(false);

    const row = await rowOf(CASE.criterion);
    expect(within(row).queryByRole("button", { name: /Run as…/ })).toBeNull();
  });
});

describe("leaving the run-as box", () => {
  it("saves what was typed rather than throwing it away", async () => {
    const actor = userEvent.setup();
    api.evaluation.cases.mockResolvedValue([CASE]);
    api.evaluation.updateCase.mockResolvedValue({ ...CASE, run_as: "an@cty.vn" });
    draw();
    const row = await rowOf(CASE.criterion);

    await actor.click(within(row).getByRole("button", { name: /Run as…/ }));
    await actor.type(within(row).getByLabelText(/Run as/), "an@cty.vn");
    await actor.tab();

    await waitFor(() => expect(api.evaluation.updateCase).toHaveBeenCalledWith(1, 2, 7, { run_as: "an@cty.vn" }));
  });
});
