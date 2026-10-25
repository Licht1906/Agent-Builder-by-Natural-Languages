import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ApiError, request } from "./api";

/* `messageOf` is not exported, and testing it directly would mean exporting a
   function only a test wants. It is reached through `request` instead — with
   `fetch` stubbed — which is also the honest seam: what matters is not that a
   formatter returns a string, it is that a refusal coming back over HTTP turns
   into a sentence a person can read. */

function respondWith(status, body) {
  global.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  });
}

describe("request — turning a refusal into a readable sentence", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reads FastAPI's plain-sentence detail", async () => {
    respondWith(403, { detail: "Chỉ admin mới truy cập được" });

    await expect(request("/admin/users")).rejects.toThrow("Chỉ admin mới truy cập được");
  });

  it("reads pydantic's {loc, msg} field list", async () => {
    respondWith(422, {
      detail: [{ loc: ["body", "email"], msg: "value is not a valid email address" }],
    });

    await expect(request("/auth/register", { method: "POST", body: {} })).rejects.toThrow(
      "email: value is not a valid email address"
    );
  });

  it("reads UC-1's {code, path, message} problem list", async () => {
    // The regression this test exists for: read as pydantic's shape, a
    // `Problem` has no `msg`, so every one of them rendered as the literal
    // string "undefined" — a field-level refusal looking like a frontend bug.
    respondWith(409, {
      detail: [
        { code: "sandbox_run_missing", path: "metadata.status", message: "chưa có lượt chạy sandbox nào (FR-38)" },
      ],
    });

    let thrown;
    try {
      await request("/agents/agt_x/versions/1/submit-for-approval", { method: "POST" });
    } catch (err) {
      thrown = err;
    }

    expect(thrown).toBeInstanceOf(ApiError);
    expect(thrown.message).toContain("chưa có lượt chạy sandbox nào");
    expect(thrown.message).not.toContain("undefined");
  });

  it("joins several problems into one line", async () => {
    respondWith(409, {
      detail: [
        { code: "sandbox_run_missing", path: "metadata.status", message: "chưa có lượt chạy sandbox" },
        { code: "deepeval_below_threshold", path: "metadata.status", message: "chưa đạt min_pass_rate" },
      ],
    });

    await expect(request("/x", { method: "POST" })).rejects.toThrow(
      /chưa có lượt chạy sandbox.*chưa đạt min_pass_rate/
    );
  });

  it("keeps the parsed body on the error so a screen can render the list itself", async () => {
    respondWith(409, { detail: [{ code: "a", path: "p", message: "m" }] });

    let thrown;
    try {
      await request("/x", { method: "POST" });
    } catch (err) {
      thrown = err;
    }

    // `SubmitTab`/`LifecycleTab` both read `err.body.detail` rather than the
    // flattened message, so this shape is load-bearing, not incidental.
    expect(thrown.status).toBe(409);
    expect(thrown.body.detail).toHaveLength(1);
  });

  it("says the session expired on a 401 rather than echoing a status code", async () => {
    respondWith(401, {});

    await expect(request("/auth/me")).rejects.toThrow(/hết hạn/);
  });

  it("returns null for 204 instead of trying to parse an empty body", async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 204, text: async () => "" });

    await expect(request("/x", { method: "DELETE" })).resolves.toBeNull();
  });
});
