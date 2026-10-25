import { LanguageToggle, ThemeToggle } from "../components/ui";
import { BrandLockup } from "../components/brand";
import { AgentMascot } from "../components/mascot";
import { useLang } from "../i18n";

const FEATURES = [
  {
    icon: `<svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M15 4V2m0 2v2m0-2h-4.5M3 10v9a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-9H3ZM3 10V6a2 2 0 0 1 2-2h2"/><path d="M7 2v4"/><rect x="7" y="14" width="4" height="4" rx=".5"/></svg>`,
    title: "AI Solution Designer",
    desc: "Describe the task in plain language. The designer interviews you to fill in goals, data, actions, and expected output before anything gets built.",
  },
  {
    icon: `<svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M12 2 2 7l10 5 10-5-10-5ZM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>`,
    title: "Template-guided workflows",
    desc: "Specifications turn into a draft workflow from a vetted template, not a free-form guess — then you adjust it on the canvas.",
  },
  {
    icon: `<svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="m9 15 2 2 4-4"/></svg>`,
    title: "Sandbox + auto evaluation",
    desc: "Test every step with mock tools, then let rule-based and LLM-graded checks confirm it behaves before you publish.",
  },
];

const STATS = [
  { n: "7 fields", label: "Interview before any code" },
  { n: "8 steps", label: "Node types on the canvas" },
  { n: "5 gates", label: "Passed before publishing" },
];

const GATES = [
  { no: "01", title: "Spec", body: "The designer interviews you, fills seven fields, and flags contradictions before a line of template code is generated." },
  { no: "02", title: "Workflow", body: "A canvas of typed nodes — LLM call, tool use, branch, human gate — wired from the spec, then hand-tuned." },
  { no: "03", title: "Sandbox", body: "Run every step against mock tools with recorded inputs; no real user is exposed to untested logic." },
  { no: "04", title: "Evaluation", body: "Rule-based and LLM-graded checks on each sandbox transcript, scored per criterion you wrote in the spec." },
  { no: "05", title: "Approval", body: "An admin reviews the dossier — spec, workflow, sandbox logs, scores — and decides: approve, reject, or comment." },
];

export default function LandingPage({ goLogin, goRegister }) {
  const { t } = useLang();
  return (
    <main style={{ flex: 1, display: "flex", flexDirection: "column", background: "var(--color-bg)", color: "var(--color-text)", fontFamily: "var(--font-body)" }}>

      {/* ── HEADER ── */}
      <header style={{ display: "flex", alignItems: "center", gap: 26, padding: "var(--space-4) var(--space-10)", borderBottom: "1px solid var(--color-divider)", position: "sticky", top: 0, zIndex: 6, background: "color-mix(in srgb, var(--color-bg) 90%, transparent)", backdropFilter: "blur(8px)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginRight: "auto" }}>
          <BrandLockup size={29} />
          <span className="tag tag-outline" style={{ fontSize: "var(--text-2xs)" }}>beta</span>
        </div>
        <a href="#features" style={{ fontSize: "var(--text-md)", textDecoration: "none", color: "color-mix(in srgb, var(--color-text) 70%, transparent)" }}>{t('Docs')}</a>
        <a href="#features" style={{ fontSize: "var(--text-md)", textDecoration: "none", color: "color-mix(in srgb, var(--color-text) 70%, transparent)" }}>{t('Templates')}</a>
        <a href="#gates" style={{ fontSize: "var(--text-md)", textDecoration: "none", color: "color-mix(in srgb, var(--color-text) 70%, transparent)" }}>{t('Pricing')}</a>
        <LanguageToggle />
        <ThemeToggle />
        <button type="button" onClick={goLogin} className="btn btn-secondary" style={{ height: 36 }}>{t('Sign in')}</button>
        <button type="button" onClick={goRegister} className="btn btn-primary" style={{ height: 36 }}>{t('Get started')}</button>
      </header>

      {/* ── HERO ── */}
      <section style={{ position: "relative", overflow: "hidden", padding: "var(--space-20) var(--space-10) var(--space-16)" }}>
        {/* Animated grid background */}
        <div style={{ position: "absolute", inset: 0, opacity: 0.07, backgroundImage: "linear-gradient(var(--color-text) 1px, transparent 1px), linear-gradient(90deg, var(--color-text) 1px, transparent 1px)", backgroundSize: "46px 46px", animation: "agDrift 11s linear infinite", pointerEvents: "none" }} />

        <div style={{ position: "relative", maxWidth: 1280, margin: "0 auto", display: "grid", gridTemplateColumns: "1.05fr .95fr", gap: 56, alignItems: "center" }}>
          {/* Left — text */}
          <div style={{ animation: "agUp .5s ease both" }}>
            <span className="tag tag-outline" style={{ gap: 7 }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--color-accent)", animation: "agPulse 1.8s ease-in-out infinite" }} />
              {t('Now in public beta — describe a task, ship an agent')}
            </span>
            <h1 className="ab-display ab-display-xl" style={{ margin: "var(--space-4) 0 var(--space-5)", maxWidth: "13ch" }}>
              {t("Build agents that")} <span style={{ color: "var(--color-accent-700)" }}>{t("actually work")}</span>
            </h1>
            <p style={{ margin: "0 0 32px", maxWidth: "54ch", fontSize: "var(--text-xl)", lineHeight: 1.62, color: "color-mix(in srgb, var(--color-text) 66%, transparent)", textWrap: "pretty" }}>
              {t('Tell the AI Solution Designer what you need. It interviews you, drafts a spec, generates a starter workflow from a vetted template, and hands you a canvas to fine-tune before you ship.')}
            </p>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <button type="button" onClick={goRegister} className="btn btn-primary" style={{ height: 44, paddingInline: "var(--space-6)", fontSize: "var(--text-lg)" }}>{t('Start building free')}</button>
              <button type="button" onClick={goLogin} className="btn btn-secondary" style={{ height: 44, paddingInline: "var(--space-5)", fontSize: "var(--text-lg)" }}>{t('I already have an account')}</button>
            </div>
            <div style={{ display: "flex", gap: 36, marginTop: 38, paddingTop: "var(--space-6)", borderTop: "1px solid var(--color-divider)", flexWrap: "wrap" }}>
              {STATS.map((st) => (
                <div key={t(st.label)}>
                  <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)", lineHeight: 1, color: "var(--color-accent)" }}>{t(st.n)}</p>
                  <p style={{ margin: "3px 0 0", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>{t(st.label)}</p>
                </div>
              ))}
            </div>
          </div>

          {/* Right — Blueprint mockup */}
          <div className="blueprint" style={{ padding: "var(--space-5)", animation: "agUp .5s .1s ease both" }}>
            <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
            {/* Leaning over the frame rather than sitting inside it: the card is a
                picture of the designer *asking*, and the mascot is the one asking.
                Above the corner marks, clear of the text, and out of the tab order
                and the accessibility tree — the conversation below says all of it
                in words already. */}
            <AgentMascot state="builder" size={168} style={{ position: "absolute", top: -96, right: 4, zIndex: 2, pointerEvents: "none" }} />
            <div style={{ display: "flex", alignItems: "center", gap: 8, paddingBottom: "var(--space-3)", borderBottom: "1px solid var(--color-divider)", marginBottom: 14 }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--color-accent)", animation: "agPulse 1.8s ease-in-out infinite" }} />
              <span style={{ fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>{t('AI Solution Designer — sample')}</span>
            </div>
            <p style={{ margin: "0 0 14px", padding: "var(--space-3)", fontSize: "var(--text-md)", lineHeight: 1.55, border: "1px solid var(--color-divider)", maxWidth: "90%" }}>{t('Who should this agent serve, and does its action need approval first?')}</p>
            <p style={{ margin: "0 0 16px", padding: "var(--space-3)", fontSize: "var(--text-md)", lineHeight: 1.55, background: "var(--color-accent)", color: "var(--color-bg)", maxWidth: "86%", marginLeft: "auto" }}>{t('Support agents. Drafting a reply is fine; sending needs my approval.')}</p>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 5 }}>
              {[1,2,3,4,5,6,7].map((i) => (
                <span key={i} style={{ height: 4, background: i <= 4 ? "var(--color-accent)" : "color-mix(in srgb, var(--color-text) 12%, transparent)" }} />
              ))}
            </div>
            <p style={{ margin: "10px 0 0", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>{t('4 of 7 fields answered')}</p>
          </div>
        </div>
      </section>

      {/* ── FEATURES ── */}
      <section id="features" style={{ maxWidth: 1280, width: "100%", margin: "0 auto", padding: "0 var(--space-10) var(--space-20)" }}>
        <h2 style={{ margin: "0 0 6px", fontSize: "var(--text-3xl)" }}>{t('Three things it does that a chat box does not')}</h2>
        <p style={{ margin: "0 0 28px", fontSize: "var(--text-lg)", color: "color-mix(in srgb, var(--color-text) 60%, transparent)", maxWidth: "66ch" }}>{t('Every agent goes through the same gates: spec, workflow, sandbox, evaluation, approval.')}</p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)" }}>
          {FEATURES.map((f) => (
            <div key={t(f.title)} style={{ background: "var(--color-bg)", padding: "var(--space-6) var(--space-5)", display: "flex", flexDirection: "column", gap: 10, minHeight: 212 }}>
              <span style={{ width: 20, height: 20, display: "block", color: "var(--color-accent)" }} dangerouslySetInnerHTML={{ __html: f.icon }} />
              <p style={{ margin: "4px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)" }}>{t(f.title)}</p>
              <p style={{ margin: 0, fontSize: "var(--text-md)", lineHeight: 1.65, color: "color-mix(in srgb, var(--color-text) 62%, transparent)" }}>{t(f.desc)}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ── GATES ── */}
      <section id="gates" style={{ background: "var(--color-accent-900)", color: "#f2f2f3", padding: "var(--space-16) var(--space-10)" }}>
        <div style={{ maxWidth: 1280, margin: "0 auto" }}>
          <h2 style={{ margin: "0 0 6px", fontSize: "var(--text-3xl)", color: "#f2f2f3" }}>{t('Five gates before anything ships')}</h2>
          <p style={{ margin: "0 0 30px", fontSize: "var(--text-lg)", opacity: 0.72, maxWidth: "62ch" }}>{t('Nothing reaches a real user until each one has been passed — and each gate is a screen, not a policy document.')}</p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 1, background: "color-mix(in srgb, #f2f2f3 22%, transparent)", border: "1px solid color-mix(in srgb, #f2f2f3 22%, transparent)" }}>
            {GATES.map((g) => (
              <div key={g.no} style={{ background: "var(--color-accent-900)", padding: "var(--space-5) var(--space-4)", minHeight: 150 }}>
                <p style={{ margin: "0 0 8px", fontFamily: "var(--font-heading)", fontSize: "var(--text-3xl)", lineHeight: 1, opacity: 0.5 }}>{g.no}</p>
                <p style={{ margin: "0 0 5px", fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", color: "#f2f2f3" }}>{t(g.title)}</p>
                <p style={{ margin: 0, fontSize: "var(--text-sm)", lineHeight: 1.55, opacity: 0.72 }}>{t(g.body)}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── CTA ── */}
      <section style={{ borderTop: "1px solid var(--color-divider)", padding: "var(--space-12) var(--space-10)" }}>
        <div style={{ maxWidth: 1280, margin: "0 auto", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 30, flexWrap: "wrap" }}>
          <div>
            <h2 style={{ margin: "0 0 4px", fontSize: "var(--text-3xl)" }}>{t('Describe a task, ship an agent')}</h2>
            <p style={{ margin: 0, fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 60%, transparent)" }}>{t('Demo credentials are pre-filled on the sign-in screen.')}</p>
          </div>
          <button type="button" onClick={goRegister} className="btn btn-primary" style={{ height: 46, paddingInline: "var(--space-8)", fontSize: "var(--text-lg)" }}>{t('Start building free')}</button>
        </div>
      </section>

      {/* ── FOOTER ── */}
      <footer style={{ borderTop: "1px solid var(--color-divider)", padding: "var(--space-5) var(--space-10)", display: "flex", gap: 18, flexWrap: "wrap", alignItems: "center", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>
        <span style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-md)", color: "var(--color-text)" }}>Agentlab</span>
        <span style={{ marginLeft: "auto" }}>© 2026 Agentlab, Inc.</span>
      </footer>
    </main>
  );
}
