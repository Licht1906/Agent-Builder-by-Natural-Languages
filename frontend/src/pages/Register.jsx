import { useState } from "react";
import { ArrowRight, Eye, EyeOff, MailCheck } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Spinner } from "../components/ui";
import AuthShell from "./AuthShell";
import { useLang } from "../i18n";

const MIN_PASSWORD = 12;

const ASIDE = (
  <>
    <div className="ab-card" style={{ padding: "var(--space-5)" }}>
      <p className="ab-mono" style={{ fontSize: "var(--text-2xs)", color: "var(--ab-accent)", marginBottom: 10 }}>WHAT YOUR ROLE DECIDES</p>
      <p style={{ fontSize: "var(--text-sm)", lineHeight: 1.6, margin: 0 }}>
        A <b>Creator</b> builds agents and owns everything they build. An <b>Admin</b> reviews
        other people's work and decides what gets deployed.
      </p>
    </div>
    <p style={{ marginTop: 26, fontSize: "var(--text-sm)", color: "var(--ab-muted)", lineHeight: 1.6 }}>
      Your role is fixed on the account and enforced on every request — it is not a setting you can
      flip later in the interface.
    </p>
  </>
);

/** The confirmation panel a successful signup lands on — there is no session
 *  to hand back (every new account is `pending`, this form's requested role
 *  included), so this replaces the old auto-sign-in with the one thing that
 *  is actually true next: somebody with an active admin account has to act. */
function PendingActivationNotice({ email, goLogin }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="ab-card" style={{ padding: "var(--space-4)", display: "flex", gap: 12, alignItems: "flex-start" }}>
        <MailCheck size={18} style={{ color: "var(--ab-primary)", flexShrink: 0, marginTop: 2 }} />
        <p style={{ margin: 0, fontSize: "var(--text-sm)", lineHeight: 1.6 }}>
          Account created for <b>{email}</b>. It stays <b>pending</b> until an admin activates it —
          you can't sign in yet.
        </p>
      </div>
      <button className="ab-btn ab-btn-primary" onClick={goLogin}>
        Go to sign in<ArrowRight size={15} />
      </button>
    </div>
  );
}

export default function RegisterPage({ goLanding, goLogin }) {
  const { t } = useLang();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [role, setRole] = useState("creator");
  const [jobTitle, setJobTitle] = useState("");
  const [department, setDepartment] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [registeredEmail, setRegisteredEmail] = useState(null);

  /* Checked here so an obvious mistake costs no round trip. Everything that is
     actually a *rule* — is this address taken, is this employee_id already
     somebody else's — is only ever answered by the backend, because only the
     backend knows. */
  function localProblem() {
    if (!email.trim()) return t("Enter your email address.");
    if (password.length < MIN_PASSWORD) return t("Password must be at least {n} characters.", { n: MIN_PASSWORD });
    if (password !== confirm) return t("The two passwords do not match.");
    if (!jobTitle.trim()) return t("Enter your job title.");
    if (!department.trim()) return t("Enter your department.");
    return "";
  }

  async function submit(event) {
    event.preventDefault();
    const problem = localProblem();
    setError(problem);
    if (problem) return;

    setLoading(true);
    try {
      const user = await api.auth.register({
        email: email.trim(),
        password,
        role,
        job_title: jobTitle.trim(),
        department: department.trim(),
      });
      setRegisteredEmail(user.email);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  if (registeredEmail) {
    return (
      <AuthShell
        goLanding={goLanding}
        badge="ACCOUNT PENDING"
        title="Almost there"
        subtitle="One more step before you can sign in."
        aside={ASIDE}
      >
        <PendingActivationNotice email={registeredEmail} goLogin={goLogin} />
      </AuthShell>
    );
  }

  return (
    <AuthShell
      goLanding={goLanding}
      badge="Create account"
      title="Start building"
      subtitle="Pick the role this account will have — it decides what you can do."
      footer={
        <p style={{ margin: "20px 0 0", paddingTop: "var(--space-4)", borderTop: "1px solid var(--color-divider)", fontSize: "var(--text-sm)", textAlign: "center", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
          {t("Already have an account?")}{" "}
          <button type="button" onClick={goLogin} className="btn btn-ghost" style={{ fontSize: "var(--text-sm)" }}>{t('Sign in')}</button>
        </p>
      }
    >
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {/* Role — segmented control */}
        <div className="field">
          <label>{t('Role')}</label>
          <div className="seg" style={{ width: "100%" }}>
            <label className="seg-opt" style={{ flex: 1, justifyContent: "center" }}>
              <input type="radio" name="role" checked={role === "creator"} onChange={() => setRole("creator")} />{t("Creator")}
            </label>
            <label className="seg-opt" style={{ flex: 1, justifyContent: "center" }}>
              <input type="radio" name="role" checked={role === "admin"} onChange={() => setRole("admin")} />{t("Admin")}
            </label>
          </div>
        </div>

        <div className="field">
          <label htmlFor="reg-email">{t('Email')}</label>
          <input id="reg-email" className="input" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" />
        </div>

        <div style={{ display: "flex", gap: 12 }}>
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="reg-job-title">{t('Job title')}</label>
            <input id="reg-job-title" className="input" type="text" autoComplete="organization-title" value={jobTitle} onChange={(e) => setJobTitle(e.target.value)} placeholder={t("e.g. Backend Engineer")} />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="reg-department">{t('Department')}</label>
            <input id="reg-department" className="input" type="text" value={department} onChange={(e) => setDepartment(e.target.value)} placeholder={t("e.g. Engineering")} />
          </div>
        </div>

        <div className="field">
          <label htmlFor="reg-pw">{t('Password')}</label>
          <div style={{ display: "flex", gap: 6 }}>
            <input
              id="reg-pw"
              className="input"
              type={showPass ? "text" : "password"}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t("At least {n} characters", { n: MIN_PASSWORD })}
            />
            <button type="button" onClick={() => setShowPass((v) => !v)} className="btn btn-secondary btn-icon" aria-label={t("Show password")} style={{ flexShrink: 0 }}>
              {showPass ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
        </div>

        <div className="field">
          <label htmlFor="reg-pw2">{t('Confirm password')}</label>
          <input id="reg-pw2" className="input" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={t("Type it again")} />
        </div>

        <ErrorNote>{error}</ErrorNote>
        <button type="submit" disabled={loading} className="btn btn-primary btn-block" style={{ height: 40 }}>
          {loading ? <><Spinner />{t("Creating account…")}</> : t("Create account")}
        </button>
      </form>
    </AuthShell>
  );
}
