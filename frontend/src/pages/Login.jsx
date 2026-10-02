import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Spinner } from "../components/ui";
import AuthShell from "./AuthShell";
import { useLang } from "../i18n";

/* ============================================================
   The seeded demo accounts — development builds only.

   One row per role the platform seeds (`src/config.py`, and the migrations
   that create them): click a row and you are signed in as that person. Six
   roles see six genuinely different apps here — a creator has a workspace, a
   PM has the control room, a sales rep can only talk to agents whose audience
   names their role — and there is no other way to see the fifth and sixth of
   those without registering an account by hand and activating it.

   **The gate is `import.meta.env.DEV`, and it is load-bearing.** A list of
   working credentials with a one-click sign-in is a hazard in a production
   bundle whatever the passwords are, because the seeds are defaults and
   defaults survive deployments. `import.meta.env.DEV` is chosen over a
   `VITE_*` flag for one specific reason: Vite substitutes it as the literal
   `false` at build time, so `false ? [...] : []` constant-folds and the array
   — emails, passwords and all — is dropped from the bundle by the minifier. A
   `VITE_` flag is a *string comparison at runtime*, which means the credential
   literals ship no matter how the flag is set, and are one devtools breakpoint
   from being read. Ternary rather than a guard inside the component for the
   same reason: the literals have to be inside the branch that folds away, not
   merely unreferenced by it.

   Verified after every build by grepping `dist/` for one of the passwords.
   ============================================================ */
const DEV_ACCOUNTS = import.meta.env.DEV
  ? [
      { email: "creator@example.com", password: "creator-dev-password", role: "creator", what: "Builds agents" },
      { email: "creator2@example.com", password: "creator2-dev-password", role: "creator", what: "Owns the Drive agent" },
      { email: "admin@example.com", password: "admin-dev-password", role: "admin", what: "Platform settings, review queue" },
      { email: "agent-admin@example.com", password: "agent-admin-dev-password", role: "agent_admin", what: "Enterprise review queue" },
      { email: "sales.rep@demo.local", password: "sales-rep-dev-password", role: "sales_rep", what: "Uses agents — own rows only" },
      { email: "sales.manager@demo.local", password: "sales-manager-dev-password", role: "sales_manager", what: "Uses agents — team rows" },
    ]
  : [];

export default function LoginPage({ onSignedIn, goLanding, goRegister }) {
  const { t } = useLang();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  /* One sign-in path, whether the credentials were typed or clicked: a demo
     row that took a shortcut around the real call would be a shortcut around
     the thing it is there to exercise. */
  async function signIn(withEmail, withPassword) {
    setError("");
    setLoading(true);
    try {
      const token = await api.auth.login(withEmail, withPassword);
      api.setToken(token.access_token);
      onSignedIn(await api.auth.me());
    } catch (err) {
      api.clearToken();
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError(t("Please fill in both fields."));
      return;
    }
    await signIn(email.trim(), password);
  }

  return (
    <AuthShell
      goLanding={goLanding}
      badge="Secure login"
      title="Welcome back"
      subtitle="Sign in to your Agentlab workspace."
      footer={
        <p style={{ margin: "20px 0 0", paddingTop: "var(--space-4)", borderTop: "1px solid var(--color-divider)", fontSize: "var(--text-sm)", textAlign: "center", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
          {t("No account?")}{" "}
          <button type="button" onClick={goRegister} className="btn btn-ghost" style={{ fontSize: "var(--text-sm)" }}>{t('Create one free')}</button>
        </p>
      }
    >
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div className="field">
          <label htmlFor="email">{t('Email')}</label>
          <input
            id="email"
            className="input"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@company.com"
          />
        </div>
        <div className="field">
          <label htmlFor="pw">{t('Password')}</label>
          <div style={{ display: "flex", gap: 6 }}>
            <input
              id="pw"
              className="input"
              type={showPass ? "text" : "password"}
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••••••"
            />
            <button
              type="button"
              onClick={() => setShowPass((v) => !v)}
              className="btn btn-secondary btn-icon"
              aria-label={t("Show password")}
              style={{ flexShrink: 0 }}
            >
              {showPass ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
        </div>
        <ErrorNote>{error}</ErrorNote>
        <button type="submit" disabled={loading} className="btn btn-primary btn-block" style={{ height: 40 }}>
          {loading ? <><Spinner />{t("Signing in…")}</> : t("Sign in")}
        </button>
      </form>

      {DEV_ACCOUNTS.length > 0 && (
        <div style={{ marginTop: "var(--space-5)", paddingTop: "var(--space-4)", borderTop: "1px dashed var(--color-divider)" }}>
          <p className="ab-mono" style={{ margin: "0 0 var(--space-2)", fontSize: "var(--text-2xs)", letterSpacing: ".13em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 45%, transparent)" }}>
            {t("Sample accounts")} · {t("dev build only")}
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)" }}>
            {DEV_ACCOUNTS.map((account) => (
              <button
                key={account.email}
                type="button"
                disabled={loading}
                /* What this role actually sees, one hover away rather than a third
                   column in a 392px panel — the same bargain every explanation
                   in this app makes (ADR-0056 §8). */
                title={account.what}
                onClick={() => signIn(account.email, account.password)}
                style={{
                  display: "flex", alignItems: "baseline", gap: "var(--space-2)", width: "100%",
                  padding: "var(--space-2) var(--space-3)", border: 0, background: "var(--color-bg)",
                  color: "var(--color-text)", font: "inherit", fontSize: "var(--text-xs)", textAlign: "left",
                }}
              >
                <span className="ab-mono" style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{account.email}</span>
                <span style={{ flex: "none", color: "var(--color-accent)" }}>{account.role}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </AuthShell>
  );
}
