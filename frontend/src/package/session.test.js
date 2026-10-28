import { beforeEach, describe, expect, it, vi } from "vitest";
import { authHeaders, beginLogin, claimsOf, completeLogin, currentToken, keepToken, takeFragmentToken } from "./session";

const OIDC = { issuer: "https://idp.cty.test", client_id: "tro-ly-cong-no", scope: "openid email" };
const DISCOVERY = { authorization_endpoint: "https://idp.cty.test/auth", token_endpoint: "https://idp.cty.test/token" };

/** A JWT-shaped string with `claims` — unsigned, which is all the page ever reads (the runtime verifies). */
function tokenWith(claims) {
  const utf8 = (text) => String.fromCharCode(...new TextEncoder().encode(text));
  const part = (value) => btoa(utf8(JSON.stringify(value))).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${part({ alg: "RS256" })}.${part(claims)}.signature`;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const at = (search = "") => ({ origin: "http://goi.cty", pathname: "/", search, hash: "", assign: vi.fn() });

beforeEach(() => sessionStorage.clear());

describe("OIDC sign-in (Authorization Code + PKCE)", () => {
  it("sends the person to the issuer with a PKCE challenge of the verifier it keeps, and a state", async () => {
    const location = at();
    await beginLogin(OIDC, { fetcher: async () => json(DISCOVERY), location });

    const sent = new URL(location.assign.mock.calls[0][0]);
    const pending = JSON.parse(sessionStorage.getItem("agent-package.oidc-pending"));
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(pending.verifier)));
    const expected = btoa(String.fromCharCode(...digest)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

    expect(sent.origin + sent.pathname).toBe("https://idp.cty.test/auth");
    expect(Object.fromEntries(sent.searchParams)).toEqual({
      response_type: "code",
      client_id: "tro-ly-cong-no",
      redirect_uri: "http://goi.cty/",
      scope: "openid email",
      state: pending.state,
      code_challenge: expected,
      code_challenge_method: "S256",
    });
  });

  it("exchanges the code with the verifier, keeps the ID token, and clears the address bar", async () => {
    sessionStorage.setItem("agent-package.oidc-pending", JSON.stringify({ verifier: "v-123", state: "s-1" }));
    const idToken = tokenWith({ email: "an@cty.vn", exp: Math.floor(Date.now() / 1000) + 600 });
    const fetcher = vi.fn(async (url) => (String(url).endsWith("openid-configuration") ? json(DISCOVERY) : json({ id_token: idToken, access_token: "opaque" })));
    const history = { replaceState: vi.fn() };

    const returned = await completeLogin(OIDC, { fetcher, location: at("?code=c-9&state=s-1"), history });

    expect(returned).toBe(true);
    const [, exchange] = fetcher.mock.calls.find(([url]) => url === DISCOVERY.token_endpoint);
    expect(Object.fromEntries(new URLSearchParams(exchange.body))).toEqual({
      grant_type: "authorization_code",
      code: "c-9",
      redirect_uri: "http://goi.cty/",
      client_id: "tro-ly-cong-no",
      code_verifier: "v-123",
    });
    expect(currentToken()).toBe(idToken);
    expect(authHeaders()).toEqual({ Authorization: `Bearer ${idToken}` });
    expect(history.replaceState).toHaveBeenCalledWith(null, "", "/");
  });

  it("refuses a code that comes back without the state this tab sent", async () => {
    sessionStorage.setItem("agent-package.oidc-pending", JSON.stringify({ verifier: "v", state: "mine" }));
    const fetcher = vi.fn();

    await expect(
      completeLogin(OIDC, { fetcher, location: at("?code=c&state=someone-elses"), history: { replaceState() {} } })
    ).rejects.toThrow("không khớp");
    expect(fetcher).not.toHaveBeenCalled();
    expect(currentToken()).toBeNull();
  });

  it("says why when the issuer refused the sign-in", async () => {
    await expect(
      completeLogin(OIDC, { fetcher: vi.fn(), location: at("?error=access_denied&error_description=Sai+mật+khẩu"), history: { replaceState() {} } })
    ).rejects.toThrow("Sai mật khẩu");
  });

  it("is no return at all when the page opens without a code", async () => {
    expect(await completeLogin(OIDC, { fetcher: vi.fn(), location: at(), history: { replaceState() {} } })).toBe(false);
  });
});

describe("the token in hand", () => {
  it("is dropped once it has expired, so no request carries a dead token", () => {
    keepToken(tokenWith({ email: "an@cty.vn", exp: Math.floor(Date.now() / 1000) - 5 }));

    expect(currentToken()).toBeNull();
    expect(authHeaders()).toEqual({});
  });

  it("reads Vietnamese claims", () => {
    expect(claimsOf(tokenWith({ name: "Nguyễn Văn An" })).name).toBe("Nguyễn Văn An");
  });

  it("can be handed over in the URL fragment (jwt mode) and is wiped from the address bar", () => {
    const history = { replaceState: vi.fn() };
    const token = tokenWith({ email: "an@cty.vn" });

    expect(takeFragmentToken({ ...at(), hash: `#token=${token}` }, history)).toBe(true);
    expect(currentToken()).toBe(token);
    expect(history.replaceState).toHaveBeenCalledWith(null, "", "/");
  });
});
