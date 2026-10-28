import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
      sample: vi.fn(), loadSample: vi.fn(),
    },
  };
});

/* DS-22: an agent built from the accounts-receivable templates is offered the
   sample suite they ship — UC-1's golden set in the V2 schema — in one click. */

const draw = (notify = vi.fn()) =>
  render(<LanguageProvider><EvaluationTab agentId={1} versionId={2} readsDatabase notify={notify} /></LanguageProvider>);

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem("agentlab.lang", "en");
  api.evaluation.cases.mockResolvedValue([]);
  api.evaluation.latest.mockRejectedValue(Object.assign(new Error("none"), { status: 404 }));
});

describe("the sample suite", () => {
  it("is offered, and loaded through the importer, when the template ships one", async () => {
    const actor = userEvent.setup();
    const notify = vi.fn();
    api.evaluation.sample.mockResolvedValue({ available: true, name: "Bộ đề mẫu: trợ lý công nợ", case_count: 23 });
    api.evaluation.loadSample.mockResolvedValue({ added_count: 23, skipped_count: 0, error_count: 0, rows: [], dry_run: false });
    draw(notify);

    await actor.click(await screen.findByRole("button", { name: /Load sample suite/ }));

    await waitFor(() => expect(api.evaluation.loadSample).toHaveBeenCalledWith(1));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.stringMatching(/23 case\(s\) imported/)));
    expect(api.evaluation.cases).toHaveBeenCalledTimes(2);
  });

  it("is not offered when the template ships none", async () => {
    api.evaluation.sample.mockResolvedValue({ available: false, name: null, case_count: 0 });
    draw();

    await waitFor(() => expect(api.evaluation.sample).toHaveBeenCalledWith(1));
    expect(screen.queryByRole("button", { name: /Load sample suite/ })).not.toBeInTheDocument();
  });
});
