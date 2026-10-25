import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import PMDashboard from "./PMDashboard";
import { LanguageProvider } from "../i18n";
import * as api from "../lib/api";

/* What is worth asserting about this page is not the arithmetic (the API does
   that, and `tests/test_api/test_pm_control_room.py` pins it) but the things
   a PM could be misled by: the board must say what is hidden and what has no
   trace; the headline must be real traffic; a filter click must ask the API
   with the widget's own word — and, now that the room is six tabs, must carry
   the reader to the Runs tab; every lever must send exactly what was clicked;
   and the review screen must keep the judge's opinion out of sight until the
   person has decided (slide 61). */

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    pm: {
      overview: vi.fn(),
      runs: vi.fn(),
      run: vi.fn(),
      reviewNext: vi.fn(),
      label: vi.fn(),
      versionReport: vi.fn(),
      judgeSample: vi.fn(),
      setDeploymentStatus: vi.fn(),
      feedback: vi.fn(),
      decideApproval: vi.fn(),
      promoteRun: vi.fn(),
      runGate: vi.fn(),
      gateRun: vi.fn(),
    },
  };
});

const rate = (n, passed) => ({ n, passed, rate: n ? passed / n : null });

const overview = () => ({
  window_days: 28,
  include_simulated: false,
  generated_at: new Date().toISOString(),
  thresholds: {
    production_success_min: 0.8, offline_online_gap_max: 0.15, thumbs_up_min: 0.7, latency_p90_max_seconds: 60,
    cost_per_run_max_usd: 0.05, judge_tpr_min: 0.85, judge_tnr_min: 0.85, calibration_min_labels: 30, noise_delta: 0.02,
    regression_drop: 0.05, judge_sample_rate: 0.1, small_n: 20,
  },
  simulation: { simulated_runs: 120, total_runs: 143, simulated_pending_approvals: 1, shown: false },
  traces: { runs: 23, with_trace: 11 },
  alerts: [{ code: "Q2", severity: "warning", message: "Nhắc công nợ v13: eval tụt 60% so với bản trước 90%.", value: 0.6, threshold: 0.9, agent_key: "7" }],
  success_by_trigger: [
    { trigger: "chat_test", runs: 20, successes: 18, simulated: 0, rate: 0.9 },
    { trigger: "manual", runs: 60, successes: 50, simulated: 0, rate: 0.8333 },
  ],
  outcomes: {
    slices: [{ outcome: "success", runs: 94 }, { outcome: "spec_error", runs: 20 }, { outcome: "system_error", runs: 9 }],
    self_fix_ratio: 0.69,
    self_fix_healthy_above: 0.6,
    unclassified_errors: 0,
    success_with_tool_failures: 2,
  },
  cost_by_agent: [
    { agent_key: "7", agent_name: "Nhắc công nợ", pipeline: "v2", agent_id: 7, runs: 43, successes: 26, cost_usd: 2.3, cost_per_run: 0.053, cost_per_success: 0.088, latency_p50: 4.2, latency_p90: 75, deployment_status: "active" },
    { agent_key: "8", agent_name: "Đối soát", pipeline: "v2", agent_id: 8, runs: 10, successes: 5, cost_usd: 0.06, cost_per_run: 0.006, cost_per_success: 0.012, latency_p50: 2, latency_p90: 3, deployment_status: null },
  ],
  cost_by_model: [{ model: "gpt-4o-mini", runs: 100, cost_usd: 2.0, tokens: 50000, cost_per_run: 0.02 }],
  quality_by_version: [
    { agent_key: "7", version_key: "12", agent_name: "Nhắc công nợ", pipeline: "v2", agent_id: 7, version_id: 12, deployed: false, offline: rate(10, 9), offline_threshold: 0.75, offline_previous: null, online: rate(30, 27), judge: rate(5, 5), thumbs: rate(8, 6), expert: rate(0, 0), tool_grounded: rate(30, 30), regressed: false, diverged: false },
    { agent_key: "7", version_key: "13", agent_name: "Nhắc công nợ", pipeline: "v2", agent_id: 7, version_id: 13, deployed: true, offline: rate(10, 6), offline_threshold: 0.75, offline_previous: rate(10, 9), online: rate(30, 23), judge: rate(6, 4), thumbs: rate(12, 6), expert: rate(3, 1), tool_grounded: rate(30, 28), regressed: true, diverged: false },
  ],
  approvals: {
    pending: 1, decided: 9, approved: 8, rejected: 1, approval_rate: 0.889,
    avg_wait_minutes: 40, p90_wait_minutes: 120, oldest_pending_minutes: 3000,
    pending_list: [{
      id: 55, agent_id: 7, agent_name: "Nhắc công nợ", version_id: 13, node_id: "duyet", approver_role: "sales_manager",
      message: "Gửi 3 thư nhắc nợ?", input_message: "Nhắc nợ tuần này", sandbox: false, simulated: false,
      waiting_minutes: 3000, created_at: new Date().toISOString(),
    }],
  },
  feedback_by_week: [{ week_start: "2026-08-10T00:00:00", thumbs_up: 6, thumbs_down: 2, ratio: 0.75 }],
  feedback_notes: [],
  failure_slices: { by_node_type: [{ key: "mcp", runs: 14 }, { key: "agent_llm", runs: 3 }], by_action: [{ key: "gmail.send_email", runs: 9 }], by_trace_code: [{ key: "tool_failure_hidden", runs: 4 }], failed_runs: 17 },
  calibration: { labelled: 3, tp: 1, fn: 0, fp: 1, tn: 1, tpr: 1, tnr: 0.5, agreement: 0.667, kappa: 0.4, trusted: false, min_labels: 30, tpr_min: 0.85, tnr_min: 0.85, disagreements: [{ run_id: 901, agent_name: "Nhắc công nợ", judge_passed: true, expert_passed: false, judge_reason: "bám tool", expert_note: "tool hỏng mà nói đã gửi", expert_codes: ["tool_failure_hidden"] }] },
  review_queue: 7,
  activation: { agents: 5, with_confirmed_workflow: 4, with_trial_run: 3, published: 2, with_production_run: 2 },
  gate_history: [{ id: 3, mode: "simulated", passed: false, per_family: { draft: { passed: 5, total: 6, wanted: 5, out_of: 6 } }, thresholds: { _unsatisfiable_cases: ["multi-turn-01"] }, failure_classes: { case_issue: 1 }, created_at: new Date().toISOString() }],
  trace_codes: { wrong_intent: "Hiểu sai việc", tool_failure_hidden: "Tool hỏng nhưng nói xong", other: "Khác" },
});

const draw = (tab = "overview", { notify = vi.fn(), openTab = vi.fn() } = {}) => {
  render(<LanguageProvider><PMDashboard tab={tab} openTab={openTab} notify={notify} /></LanguageProvider>);
  return { notify, openTab };
};

const run = (overrides) => ({
  id: 901, pipeline: "v2", agent_key: "7", agent_name: "Nhắc công nợ", version_key: "13", agent_id: 7, version_id: 13,
  conversation_id: "real-abc", trigger: "manual", outcome: "spec_error", error: "tool_id trống", cost_usd: 0.05,
  latency_seconds: 9.1, tokens: 1200, simulated: false, has_trace: true, tool_calls: 1, tool_failures: 1,
  failure_node_type: "mcp", input_message: "Nhắc nợ tuần này", thumbs: null, judge_pass: 1, tool_grounded: 1,
  expert_passed: null, expert_codes: [], created_at: new Date().toISOString(), ...overrides,
});

const detail = (overrides) => ({
  id: 901, pipeline: "v2", agent_key: "7", version_key: "13", agent_name: "Nhắc công nợ", agent_id: 7, version_id: 13,
  trace_id: "t1", conversation_id: "real-abc", caller: "u3", trigger: "manual", outcome: "spec_error", error: "tool_id trống",
  cost_usd: 0.05, latency_seconds: 9.1, tokens: 1200, simulated: false, input_message: "Nhắc nợ tuần này",
  answer: "Đã gửi 3 thư.",
  transcript: [
    { kind: "tool", node_id: "gui", tool: "gmail.send_email", args: { to: "a@b.c" }, simulated: false },
    { kind: "tool_result", node_id: "gui", tool: "gmail.send_email", ok: false, simulated: false, content: "HTTP 502" },
    { kind: "node", node_id: "noi", node_type: "agent_llm", content: "Đã gửi 3 thư." },
  ],
  node_count: 2, tool_calls: 1, tool_failures: 1, actions: ["gmail.send_email"], first_failure: "gui:gmail.send_email",
  failure_node_type: "mcp", created_at: new Date().toISOString(),
  scores: [{ name: "tools_ok", value: 0, source: "code", comment: "Tool hỏng: gui:gmail.send_email", given_by_email: null, created_at: new Date().toISOString() }],
  judge_verdicts: [{ name: "judge:correctness", value: 1, source: "judge", comment: "bám tool", given_by_email: null, created_at: new Date().toISOString() }],
  labels: [],
  judge_hidden_until_labelled: true,
  queue_remaining: 7,
  trace_url: null,
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem("agentlab.lang", "en");
  api.pm.overview.mockResolvedValue(overview());
  api.pm.runs.mockResolvedValue([run()]);
  api.pm.run.mockResolvedValue(detail());
  api.pm.reviewNext.mockResolvedValue(detail());
});

describe("PMDashboard — overview", () => {
  it("says what is hidden and what has no trace, and carries the API's own alert sentence", async () => {
    draw();
    expect(await screen.findByText(/120 simulated runs exist and are hidden/)).toBeInTheDocument();
    expect(screen.getByText(/11 of 23 runs in this window have a trace/)).toBeInTheDocument();
    expect(screen.getByText(/eval tụt 60%/)).toBeInTheDocument();
  });

  const leadTile = async (label) => within((await screen.findByText(label)).closest("div"));

  it("leads with real traffic, not with the creator's own rehearsals", async () => {
    draw();
    const tile = await leadTile("production success");
    expect(tile.getByText("83%")).toBeInTheDocument();
    expect(tile.getByText(/60 runs · bar 80%/)).toBeInTheDocument();
  });

  it("says so plainly when nobody but the creator has used anything", async () => {
    const rehearsalsOnly = overview();
    rehearsalsOnly.success_by_trigger = rehearsalsOnly.success_by_trigger.filter((r) => r.trigger === "chat_test");
    api.pm.overview.mockResolvedValue(rehearsalsOnly);
    draw();
    const tile = await leadTile("production success");
    expect(tile.getByText("—")).toBeInTheDocument();
    expect(tile.getByText(/no real runs yet/i)).toBeInTheDocument();
  });

  it("asks the API for runs with the widget's own word when a bar is clicked, goes to the Runs tab, and reloads with simulation when toggled", async () => {
    const actor = userEvent.setup();
    const { openTab } = draw();
    await screen.findByText(/Run success rate by trigger/);
    await actor.click(screen.getByRole("button", { name: /spec_error/ }));
    await waitFor(() => expect(api.pm.runs).toHaveBeenLastCalledWith(expect.objectContaining({ outcome: "spec_error", includeSimulated: false })));
    expect(openTab).toHaveBeenCalledWith("runs");
    await actor.click(screen.getByRole("button", { name: /^simulated$/ }));
    await waitFor(() => expect(api.pm.overview).toHaveBeenLastCalledWith(28, true));
  });

  it("sends the reader to the Review tab from the hero tile, and leaves tab navigation to the sidebar", async () => {
    const actor = userEvent.setup();
    const { openTab } = draw();
    await actor.click(await screen.findByText("to review"));
    expect(openTab).toHaveBeenCalledWith("review");
    // The page used to repeat the sidebar's six tabs as a strip; two lists of
    // the same six only asked the reader which one was in charge.
    expect(screen.queryByRole("tab", { name: "Cost & latency" })).not.toBeInTheDocument();
  });

  it("takes any window the backend accepts, not only the three presets", async () => {
    const actor = userEvent.setup();
    draw();
    const field = await screen.findByLabelText(/Window, in days/);
    await actor.clear(field);
    await actor.type(field, "45{enter}");
    await waitFor(() => expect(api.pm.overview).toHaveBeenLastCalledWith(45, false));
    await waitFor(() => expect(api.pm.runs).toHaveBeenLastCalledWith(expect.objectContaining({ windowDays: 45 })));

    // Out of range is clamped rather than sent — the API would 422 on 0.
    await actor.clear(field);
    await actor.type(field, "0{enter}");
    await waitFor(() => expect(api.pm.overview).toHaveBeenLastCalledWith(1, false));

    // The presets are still one click.
    await actor.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(api.pm.overview).toHaveBeenLastCalledWith(7, false));
  });
});

describe("PMDashboard — runs & traces", () => {
  it("keeps the judge's opinion hidden until the person has decided, then files the label with its codes", async () => {
    const actor = userEvent.setup();
    api.pm.label.mockResolvedValue({ label_id: 1, run_id: 901, passed: false, codes: ["tool_failure_hidden"], queue_remaining: 6 });
    draw("runs");
    await actor.click((await screen.findAllByRole("button", { name: /^read$/ }))[0]);
    expect(await screen.findByText(/Hidden until you decide/)).toBeInTheDocument();
    expect(screen.getByText(/the person asked/)).toBeInTheDocument();
    expect(screen.queryByText("bám tool")).not.toBeInTheDocument();
    // The trace reads as words: the failed tool call is named as such.
    expect(screen.getByText(/tool failed/)).toBeInTheDocument();
    // No trace backend configured → no link to invent.
    expect(screen.queryByRole("link", { name: /Langfuse/ })).not.toBeInTheDocument();

    await actor.click(screen.getByRole("button", { name: /^Fail$/ }));
    expect(screen.getByText("bám tool")).toBeInTheDocument(); // decided — now the judge may speak
    await actor.click(screen.getByRole("button", { name: "tool_failure_hidden" }));
    await actor.type(screen.getByPlaceholderText(/Why\?/), "tool hỏng mà nói đã gửi");
    await actor.click(screen.getByRole("button", { name: /Save label/ }));
    await waitFor(() => expect(api.pm.label).toHaveBeenCalledWith(901, false, ["tool_failure_hidden"], "tool hỏng mà nói đã gửi"));
  });

  it("links the run to its Langfuse trace under the same trace_id when a backend is configured", async () => {
    const actor = userEvent.setup();
    api.pm.run.mockResolvedValue(detail({ trace_url: "http://localhost:3000/project/agentlab-local/traces/t1" }));
    draw("runs");
    await actor.click((await screen.findAllByRole("button", { name: /^read$/ }))[0]);
    const link = await screen.findByRole("link", { name: /open in Langfuse/ });
    expect(link).toHaveAttribute("href", "http://localhost:3000/project/agentlab-local/traces/t1");
  });

  it("promotes a run with the input already filled from the trace", async () => {
    const actor = userEvent.setup();
    api.pm.promoteRun.mockResolvedValue({ case_id: 5, version_id: 13, golden_line: "{}", note: "Đã thêm case vào version." });
    draw("runs");
    await actor.click(await screen.findByRole("button", { name: /case/ }));
    const input = screen.getByPlaceholderText(/What the user asked/);
    expect(input).toHaveValue("Nhắc nợ tuần này");
    await actor.type(screen.getByPlaceholderText(/What a correct answer must do/), "Không nói đã gửi khi tool hỏng");
    await actor.click(screen.getByRole("button", { name: /Add as eval case/ }));
    await waitFor(() => expect(api.pm.promoteRun).toHaveBeenCalledWith(901, "Nhắc nợ tuần này", "Không nói đã gửi khi tool hỏng"));
  });
});

describe("PMDashboard — quality by version", () => {
  it("flags the regressed version and the gate cases no builder can pass", async () => {
    draw("quality");
    expect(await screen.findByText(/regressed/)).toBeInTheDocument();
    expect(screen.getByText(/cannot pass even with a perfect builder/)).toBeInTheDocument();
  });

  it("judges a sample after confirmation", async () => {
    const actor = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.pm.judgeSample.mockResolvedValue({ judged: [{ run_id: 1, agent_name: "x", passed: true, verdicts: [] }], passed: 1, skipped: 0 });
    draw("quality");
    await actor.click(await screen.findByRole("button", { name: /Judge a sample/ }));
    await waitFor(() => expect(api.pm.judgeSample).toHaveBeenCalledWith(10));
  });

  it("has no scenario-grid column — the last measure on the row is links grounded (ADR-0065)", async () => {
    draw("quality");
    const rows = await screen.findAllByRole("row");
    const v13 = rows.find((r) => within(r).queryByText("v13"));
    // Column order: agent, version, offline, online, judge, thumbs, expert, links grounded, actions.
    const cells = within(v13).getAllByRole("cell");
    expect(cells).toHaveLength(9);
    expect(cells[7]).toHaveTextContent("93%");
    expect(screen.queryByText(/phủ lưới|grid coverage/i)).toBeNull();
  });
});

describe("PMDashboard — review", () => {
  it("asks for the next run in the queue, shows how many are left, and asks again after a label", async () => {
    const actor = userEvent.setup();
    api.pm.label.mockResolvedValue({ label_id: 1, run_id: 901, passed: true, codes: [], queue_remaining: 6 });
    draw("review");
    expect(await screen.findByText(/Review — 7 left/)).toBeInTheDocument();
    expect(api.pm.reviewNext).toHaveBeenCalledWith(false);
    expect(screen.getByText(/Hidden until you decide/)).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: /^Pass$/ }));
    await actor.click(screen.getByRole("button", { name: /Save & next/ }));
    await waitFor(() => expect(api.pm.label).toHaveBeenCalledWith(901, true, [], ""));
    await waitFor(() => expect(api.pm.reviewNext).toHaveBeenCalledTimes(2));
  });

  it("says when the queue is empty instead of showing nothing", async () => {
    api.pm.reviewNext.mockResolvedValue(null);
    draw("review");
    expect(await screen.findByText(/Nothing left to review/)).toBeInTheDocument();
    expect(screen.getByText(/judge not yet trusted/)).toBeInTheDocument();
  });
});

describe("PMDashboard — cost, approvals & feedback", () => {
  it("pauses the agent in that row after confirmation, and never the unpublished one", async () => {
    const actor = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.pm.setDeploymentStatus.mockResolvedValue({ agent_id: 7, deployment_id: 1, status: "disabled", updated_at: new Date().toISOString() });
    draw("cost");
    await actor.click(await screen.findByRole("button", { name: /Pause/ }));
    await waitFor(() => expect(api.pm.setDeploymentStatus).toHaveBeenCalledWith(7, false));
    expect(screen.getByText(/not published/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Pause|Resume/ })).toHaveLength(1);
  });

  it("lets the reader drag an approval column wider and remembers it", async () => {
    localStorage.removeItem("pm.approvals.columns");
    draw("people");
    const header = (await screen.findByText("Asks")).closest("th");
    const grip = header.querySelector(".ab-col-grip");
    expect(grip).toBeTruthy();

    const widthOf = (index) => document.querySelectorAll("col")[index].style.width;
    const before = widthOf(2);
    // jsdom has no layout, so the drag is exercised through the pointer events
    // the grip actually listens to rather than through a real mouse.
    grip.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, clientX: 600 }));
    window.dispatchEvent(new MouseEvent("pointermove", { bubbles: true, clientX: 740 }));
    window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true }));

    await waitFor(() => expect(widthOf(2)).not.toBe(before));
    expect(widthOf(2)).toBe("660px"); // 520 + 140
    expect(JSON.parse(localStorage.getItem("pm.approvals.columns"))[2]).toBe(660);
  });

  it("sends an approval without a reason and a rejection with the typed one", async () => {
    const actor = userEvent.setup();
    api.pm.decideApproval.mockResolvedValue({ approval_id: 55, status: "approved", resumed: false, note: "Đã ghi quyết định." });
    const { notify } = draw("people");
    const row = (await screen.findByText("Gửi 3 thư nhắc nợ?")).closest("tr");
    await actor.click(within(row).getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(api.pm.decideApproval).toHaveBeenCalledWith(7, 55, true, null));
    expect(notify).toHaveBeenCalledWith("Đã ghi quyết định.", "ok");
    vi.spyOn(window, "prompt").mockReturnValue("khách đã trả");
    await actor.click(within(row).getByRole("button", { name: /Reject/ }));
    await waitFor(() => expect(api.pm.decideApproval).toHaveBeenLastCalledWith(7, 55, false, "khách đã trả"));
  });
});
