import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual("../../lib/api");
  return {
    ...actual,
    agents: { ...actual.agents, packageOptions: vi.fn(), exportPackage: vi.fn() },
  };
});

import * as api from "../../lib/api";
import PackageExport from "./PackageExport";

/* "Xuất gói chạy độc lập" on the Deploy tab (DS-19): locked until the version
   is approved, air-gap only when an admin provided the images, and the zip
   saved under the name the server gave it. */

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem("agentlab.lang", "en");
  URL.createObjectURL = vi.fn(() => "blob:package");
  URL.revokeObjectURL = vi.fn();
});

describe("PackageExport", () => {
  it("is locked, and says why, for a version not yet approved", async () => {
    api.agents.packageOptions.mockResolvedValue({ approved: false, airgap_available: true });
    render(<PackageExport agentId={3} versionId={7} versionStatus="pending_approval" />);

    expect(await screen.findByText(/Only an approved version/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Export standalone package/ })).toBeDisabled();
    expect(screen.getByRole("checkbox")).toBeDisabled();
  });

  it("offers air-gap images only once an admin has provided them", async () => {
    api.agents.packageOptions.mockResolvedValue({ approved: true, airgap_available: false });
    render(<PackageExport agentId={3} versionId={7} versionStatus="approved" />);

    expect(await screen.findByText(/package_runtime_images\.sh/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: /Export standalone package/ })).toBeEnabled();
  });

  it("downloads the zip for an approved version, with air-gap images when ticked", async () => {
    api.agents.packageOptions.mockResolvedValue({ approved: true, airgap_available: true });
    api.agents.exportPackage.mockResolvedValue({ blob: new Blob(["PK"]), filename: "Tro-ly-v7-goi-chay-doc-lap.zip" });
    const notify = vi.fn();
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<PackageExport agentId={3} versionId={7} versionStatus="approved" notify={notify} />);

    await waitFor(() => expect(screen.getByRole("checkbox")).toBeEnabled());
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Export standalone package/ }));

    await waitFor(() => expect(notify).toHaveBeenCalled());
    expect(api.agents.exportPackage).toHaveBeenCalledWith(3, 7, true);
    expect(clicked).toHaveBeenCalled();
    expect(clicked.mock.contexts[0].download).toBe("Tro-ly-v7-goi-chay-doc-lap.zip");
    clicked.mockRestore();
  });

  it("shows what the server refused", async () => {
    api.agents.packageOptions.mockResolvedValue({ approved: true, airgap_available: false });
    api.agents.exportPackage.mockRejectedValue(new Error("Hệ CSDL 'ERP' chưa có quy tắc truy cập"));
    const user = userEvent.setup();
    render(<PackageExport agentId={3} versionId={7} versionStatus="approved" />);

    await user.click(await screen.findByRole("button", { name: /Export standalone package/ }));

    expect(await screen.findByText(/chưa có quy tắc truy cập/)).toBeInTheDocument();
  });
});
