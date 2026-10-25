import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { LifecycleTab, SubmitTab, EditTab } from "./UC1Workbench";
import { ApiError } from "../lib/api";
import * as api from "../lib/api";

/* The whole API layer is stubbed. What these tests are about is the screen's
   own judgement — which button is reachable in which state, and what it does
   with the answer — and a real request would only make that slower to check,
   not more true. The backend's half is already covered by
   `tests/test_api/test_uc1_draft_to_queue_flow.py`, over real HTTP. */
vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    uc1Approvals: {
      deploy: vi.fn(),
      suspend: vi.fn(),
      rollback: vi.fn(),
      submitForApproval: vi.fn(),
    },
    uc1Agents: { patchVersion: vi.fn() },
  };
});

const notify = vi.fn();
const noop = async () => {};

const versionRow = (version, status) => ({ version, status, name: "Trợ lý công nợ", parent_version: null, created_at: "" });

beforeEach(() => {
  vi.clearAllMocks();
});

describe("LifecycleTab — which move is reachable in which state", () => {
  it("refuses to offer Deploy while the version is still a draft", () => {
    render(
      <LifecycleTab agentId="agt_x" version={1} status="draft" versions={[versionRow(1, "draft")]} onChanged={noop} notify={notify} />
    );

    expect(screen.getByRole("button", { name: /Deploy v1/ })).toBeDisabled();
  });

  it("offers Deploy once an approver has signed", () => {
    render(
      <LifecycleTab agentId="agt_x" version={1} status="approved" versions={[versionRow(1, "approved")]} onChanged={noop} notify={notify} />
    );

    expect(screen.getByRole("button", { name: /Deploy v1/ })).toBeEnabled();
  });

  it("keeps the kill switch disabled when nothing is actually serving", () => {
    render(
      <LifecycleTab agentId="agt_x" version={1} status="approved" versions={[versionRow(1, "approved")]} onChanged={noop} notify={notify} />
    );

    expect(screen.getByRole("button", { name: /Dừng agent/ })).toBeDisabled();
  });

  it("enables the kill switch, and names the live version, once one is deployed", () => {
    render(
      <LifecycleTab
        agentId="agt_x"
        version={2}
        status="deployed"
        versions={[versionRow(1, "approved"), versionRow(2, "deployed")]}
        onChanged={noop}
        notify={notify}
      />
    );

    expect(screen.getByRole("button", { name: /Dừng agent/ })).toBeEnabled();
    // Scoped to the "Đang phục vụ" card: `v2` also appears on the rollback
    // buttons and in the deploy copy, and an unscoped match finds all of them.
    expect(screen.getByText("Đang phục vụ").parentElement).toHaveTextContent("v2");
  });

  it("only offers rollback to versions the lifecycle actually accepts", () => {
    render(
      <LifecycleTab
        agentId="agt_x"
        version={3}
        status="deployed"
        versions={[versionRow(1, "approved"), versionRow(2, "draft"), versionRow(3, "deployed")]}
        onChanged={noop}
        notify={notify}
      />
    );

    // v1 is `approved` — a legal target. v2 is `draft` and v3 is the live one:
    // neither may be rolled back to, so neither gets a button.
    expect(screen.getByRole("button", { name: /về v1/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /về v2/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /về v3/ })).toBeNull();
  });

  it("confirms before deploying, and only then calls the endpoint", async () => {
    const user = userEvent.setup();
    api.uc1Approvals.deploy.mockResolvedValue({ status: "deployed" });
    render(
      <LifecycleTab agentId="agt_x" version={1} status="approved" versions={[versionRow(1, "approved")]} onChanged={noop} notify={notify} />
    );

    await user.click(screen.getByRole("button", { name: /Deploy v1/ }));
    // The dialog is up; nothing has been deployed yet.
    expect(api.uc1Approvals.deploy).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /Tiếp tục/ }));

    await waitFor(() => expect(api.uc1Approvals.deploy).toHaveBeenCalledWith("agt_x", 1));
  });

  it("does nothing at all when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    render(
      <LifecycleTab agentId="agt_x" version={1} status="approved" versions={[versionRow(1, "approved")]} onChanged={noop} notify={notify} />
    );

    await user.click(screen.getByRole("button", { name: /Deploy v1/ }));
    await user.click(screen.getByRole("button", { name: /Hu[ỷy]|Cancel/i }));

    expect(api.uc1Approvals.deploy).not.toHaveBeenCalled();
  });

  it("renders every problem when a lifecycle move is refused with a 409", async () => {
    const user = userEvent.setup();
    api.uc1Approvals.deploy.mockRejectedValue(
      new ApiError(409, "refused", {
        detail: [{ code: "invalid_transition", path: "metadata.status", message: "deploy chỉ hợp lệ từ 'approved'" }],
      })
    );
    render(
      <LifecycleTab agentId="agt_x" version={1} status="approved" versions={[versionRow(1, "approved")]} onChanged={noop} notify={notify} />
    );

    await user.click(screen.getByRole("button", { name: /Deploy v1/ }));
    await user.click(screen.getByRole("button", { name: /Tiếp tục/ }));

    expect(await screen.findByText(/deploy chỉ hợp lệ từ 'approved'/)).toBeInTheDocument();
    expect(screen.getByText("invalid_transition")).toBeInTheDocument();
  });

  it("suspends the agent without naming a version — the kill switch targets whatever is live", async () => {
    const user = userEvent.setup();
    api.uc1Approvals.suspend.mockResolvedValue({ status: "suspended", version: 2 });
    render(
      <LifecycleTab
        agentId="agt_x"
        version={2}
        status="deployed"
        versions={[versionRow(2, "deployed")]}
        onChanged={noop}
        notify={notify}
      />
    );

    await user.click(screen.getByRole("button", { name: /Dừng agent/ }));
    await user.click(screen.getByRole("button", { name: /Dừng$/ }));

    await waitFor(() => expect(api.uc1Approvals.suspend).toHaveBeenCalledWith("agt_x"));
  });
});

describe("SubmitTab — the gate's three conditions", () => {
  it("shows how many sandbox runs there are, because that is the condition", () => {
    render(
      <SubmitTab agentId="agt_x" version={1} status="draft" runs={[{ id: 1 }, { id: 2 }]} goldenSet={null} onSubmitted={noop} notify={notify} />
    );

    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText(/chưa chấm lần nào/)).toBeInTheDocument();
  });

  it("renders every failing condition from a 409, not just the first", async () => {
    const user = userEvent.setup();
    api.uc1Approvals.submitForApproval.mockRejectedValue(
      new ApiError(409, "refused", {
        detail: [
          { code: "sandbox_run_missing", path: "metadata.status", message: "chưa có lượt chạy sandbox nào (FR-38)" },
          { code: "deepeval_below_threshold", path: "metadata.status", message: "chưa đạt min_pass_rate (FR-33)" },
        ],
      })
    );
    render(<SubmitTab agentId="agt_x" version={1} status="draft" runs={[]} goldenSet={null} onSubmitted={noop} notify={notify} />);

    await user.click(screen.getByRole("button", { name: /Xin duyệt version 1/ }));

    expect(await screen.findByText(/còn thiếu 2 điều kiện/)).toBeInTheDocument();
    expect(screen.getByText(/chưa có lượt chạy sandbox nào/)).toBeInTheDocument();
    expect(screen.getByText(/chưa đạt min_pass_rate/)).toBeInTheDocument();
  });

  it("hides the submit button entirely once the version has moved past draft", () => {
    render(
      <SubmitTab agentId="agt_x" version={1} status="pending_approval" runs={[{ id: 1 }]} goldenSet={null} onSubmitted={noop} notify={notify} />
    );

    expect(screen.queryByRole("button", { name: /Xin duyệt/ })).toBeNull();
    expect(screen.getByText(/chỉ version/)).toBeInTheDocument();
  });

  it("reports the golden-set pass rate once one has been recorded", () => {
    render(
      <SubmitTab
        agentId="agt_x"
        version={1}
        status="draft"
        runs={[{ id: 1 }]}
        goldenSet={{ pass_rate: 0.913, passed_count: 21, total_count: 23 }}
        onSubmitted={noop}
        notify={notify}
      />
    );

    expect(screen.getByText(/91\.3%/)).toBeInTheDocument();
  });
});

describe("EditTab — an edit always becomes the next version", () => {
  it("refuses malformed JSON before sending anything", async () => {
    const user = userEvent.setup();
    render(<EditTab agentId="agt_x" version={1} onEdited={noop} notify={notify} />);

    // `fireEvent.change` rather than `user.type`: this is a JSON editor, and
    // userEvent reads `{` as the opening of a special-key sequence, so typing
    // a document character by character tests the escaping rules of the test
    // library instead of the component.
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "not json" } });
    await user.click(screen.getByRole("button", { name: /Lưu thành version 2/ }));

    expect(await screen.findByText(/JSON không hợp lệ/)).toBeInTheDocument();
    expect(api.uc1Agents.patchVersion).not.toHaveBeenCalled();
  });

  it("sends only the fields the editor names, and reports the version it landed on", async () => {
    const user = userEvent.setup();
    const onEdited = vi.fn();
    api.uc1Agents.patchVersion.mockResolvedValue({ agent_id: "agt_x", version: 2, status: "draft", spec: {} });
    render(<EditTab agentId="agt_x" version={1} onEdited={onEdited} notify={notify} />);

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: '{"metadata": {"description": "sửa"}}' },
    });
    await user.click(screen.getByRole("button", { name: /Lưu thành version 2/ }));

    await waitFor(() =>
      expect(api.uc1Agents.patchVersion).toHaveBeenCalledWith("agt_x", 1, { metadata: { description: "sửa" } })
    );
    expect(onEdited).toHaveBeenCalledWith(2);
  });
});
