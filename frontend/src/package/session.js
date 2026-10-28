/* ============================================================
   Who is chatting, in the exported package's own chat (DS-17).

   The runtime decides who is asking (`runtime.yaml`'s `auth.mode`, DS-14);
   this page only has to carry the person's credential to it:

     oidc            Authorization Code + PKCE against the company's issuer,
                     in the browser — no client secret on the page, none in
                     the package. The ID token is what the runtime verifies,
                     so `auth.oidc.audience` is the client's id.
     jwt             a token the company's identity system issued, pasted
                     once or handed over in the URL fragment (`#token=`).
     trusted_header  a gateway in front already signed the person in and
                     adds the header; the page sends nothing of its own.
     custom          the company's adapter reads the request (a cookie,
                     usually); the page sends nothing of its own either.

   Kept in `sessionStorage`: one tab, gone when it closes. No React here, so
   the flow is tested as functions.
   ============================================================ */

const TOKEN_KEY = "agent-package.token";
const PENDING_KEY = "agent-package.oidc-pending";

/** The modes where the page itself holds a bearer token. */
export const carriesToken = (mode) => mode === "oidc" || mode === "jwt";

/** `{Authorization}` for a request, or nothing — never a stale token. */
export function authHeaders() {
  const token = currentToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** The token in hand, unless it has expired (read off the JWT's `exp`, a minute early). */
export function currentToken(now = Date.now()) {
  const token = read(TOKEN_KEY);
  if (!token) return null;
  const exp = claimsOf(token)?.exp;
  if (typeof exp === "number" && exp * 1000 - 60_000 <= now) {
    signOut();
    return null;
  }
  return token;
}

export function keepToken(token) {
  write(TOKEN_KEY, token.trim());
}

export function signOut() {
  remove(TOKEN_KEY);
}

/** A JWT's payload, unverified — only to show who is signed in and when it ends. The runtime verifies. */
export function claimsOf(token) {
  try {
    const part = token.split(".")[1];
    const binary = atob(part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "="));
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, (char) => char.charCodeAt(0))));
  } catch {
    return null;
  }
}

/** `#token=…` handed over by the company's portal (jwt mode): kept, and wiped from the address bar. */
export function takeFragmentToken(location = window.location, history = window.history) {
  const match = /(?:^#|&)token=([^&]+)/.exec(location.hash || "");
  if (!match) return false;
  keepToken(decodeURIComponent(match[1]));
  history.replaceState(null, "", location.pathname + location.search);
  return true;
}

// ------------------------------------------------------------------ OIDC

/** Where the issuer sends the person back: this page, without its query. */
export const redirectUri = (location = window.location) => `${location.origin}${location.pathname}`;

async function discovery(issuer, fetcher) {
  const response = await fetcher(`${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`);
  if (!response.ok) throw new Error(`Không đọc được cấu hình OIDC của ${issuer} (HTTP ${response.status})`);
  return response.json();
}

/** Send the person to the issuer's sign-in page. Resolves only if navigation is stubbed (tests). */
export async function beginLogin(oidc, { fetcher = fetch, location = window.location, crypto = globalThis.crypto } = {}) {
  const found = await discovery(oidc.issuer, fetcher);
  const verifier = randomString(crypto);
  const state = randomString(crypto);
  write(PENDING_KEY, JSON.stringify({ verifier, state }));
  const query = new URLSearchParams({
    response_type: "code",
    client_id: oidc.client_id,
    redirect_uri: redirectUri(location),
    scope: oidc.scope || "openid email profile",
    state,
    code_challenge: await challengeOf(verifier, crypto),
    code_challenge_method: "S256",
  });
  location.assign(`${found.authorization_endpoint}?${query}`);
}

/** Back from the issuer with `?code=&state=`: exchange the code, keep the token. `false` when this is no return. */
export async function completeLogin(oidc, { fetcher = fetch, location = window.location, history = window.history } = {}) {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  const failure = params.get("error");
  if (!code && !failure) return false;
  const pending = JSON.parse(read(PENDING_KEY) || "null");
  remove(PENDING_KEY);
  history.replaceState(null, "", location.pathname);
  if (failure) throw new Error(`Đăng nhập bị từ chối: ${params.get("error_description") || failure}`);
  // A code that arrives without the state this tab sent is someone else's sign-in, replayed into ours.
  if (!pending || pending.state !== params.get("state")) throw new Error("Phiên đăng nhập không khớp — thử đăng nhập lại");
  const found = await discovery(oidc.issuer, fetcher);
  const response = await fetcher(found.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(location),
      client_id: oidc.client_id,
      code_verifier: pending.verifier,
    }).toString(),
  });
  if (!response.ok) throw new Error(`Issuer không đổi được mã đăng nhập lấy token (HTTP ${response.status})`);
  const tokens = await response.json();
  const token = tokens.id_token || tokens.access_token;
  if (!token) throw new Error("Issuer không trả về token nào");
  keepToken(token);
  return true;
}

function randomString(crypto) {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

async function challengeOf(verifier, crypto) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function base64url(bytes) {
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ------------------------------------------------------------------ storage

function read(key) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}
function write(key, value) {
  try { sessionStorage.setItem(key, value); } catch { /* private mode: the token lives as long as the page */ }
}
function remove(key) {
  try { sessionStorage.removeItem(key); } catch { /* nothing kept */ }
}
