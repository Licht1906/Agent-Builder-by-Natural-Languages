import { useLang } from "../i18n";
import { BrandLockup } from "../components/brand";
import { AgentMascot } from "../components/mascot";

/* The split-screen frame both auth screens sit in. One component rather than
   two copies of the same 40 lines: sign-in and sign-up are the same page with a
   different form in it, and the day the panel changes it should change once.

   Matches Agentlab.dc.html AUTH section: left panel accent-900 dark with grid
   drift animation, right panel form inside a .blueprint frame. */
export default function AuthShell({ goLanding, badge, title, subtitle, children, footer }) {
  const { t } = useLang();
  return (
    <main style={{ flex: 1, display: "grid", gridTemplateColumns: "1.05fr .95fr", minHeight: "100vh" }}>

      {/* ── LEFT PANEL — dark accent-900 ── */}
      <section style={{ position: "relative", overflow: "hidden", padding: "var(--space-12)", display: "flex", flexDirection: "column", justifyContent: "space-between", background: "var(--color-accent-900)", color: "#f2f2f3" }}>
        {/* Animated grid background */}
        <div style={{ position: "absolute", inset: 0, opacity: 0.26, backgroundImage: "linear-gradient(color-mix(in srgb, #f2f2f3 60%, transparent) 1px, transparent 1px), linear-gradient(90deg, color-mix(in srgb, #f2f2f3 60%, transparent) 1px, transparent 1px)", backgroundSize: "46px 46px", animation: "agDrift 10s linear infinite" }} />

        <div style={{ position: "relative" }}>
          {/* Top bar: Logo + Back to home */}
          <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
            <BrandLockup size={30} tone="light" showName={false} />
            <span style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-sm)", letterSpacing: ".22em", textTransform: "uppercase", opacity: 0.85 }}>Agentlab</span>
            <button
              type="button"
              onClick={goLanding}
              style={{ marginLeft: "auto", border: "1px solid color-mix(in srgb, #f2f2f3 40%, transparent)", background: "transparent", color: "#f2f2f3", fontFamily: "var(--font-heading)", fontSize: "var(--text-sm)", padding: "var(--space-1) var(--space-3)", cursor: "pointer" }}
            >
              {t("← Back to home")}
            </button>
          </div>

          {/* Badge + Title + Subtitle */}
          <p style={{ margin: "44px 0 0", fontSize: "var(--text-xs)", letterSpacing: ".22em", textTransform: "uppercase", opacity: 0.7 }}>{t(badge)}</p>
          <h1 className="ab-display ab-display-xl" style={{ margin: "var(--space-2) 0 var(--space-3)", maxWidth: "12ch", color: "#f2f2f3" }}>{t(title)}</h1>
          <p style={{ margin: 0, maxWidth: "42ch", fontSize: "var(--text-lg)", opacity: 0.8 }}>{t(subtitle)}</p>
        </div>

        {/* Designer sample card */}
        <div style={{ position: "relative", border: "1px solid color-mix(in srgb, #f2f2f3 26%, transparent)", padding: "var(--space-5)", marginTop: 36 }}>
          {/* Same idea as the landing hero: the sample below is a question the
              designer asks, so the asker stands next to it. */}
          <AgentMascot state="thinking" size={140} style={{ position: "absolute", top: -84, right: 8, zIndex: 2, pointerEvents: "none" }} />
          <p style={{ margin: "0 0 10px", fontSize: "var(--text-2xs)", letterSpacing: ".16em", textTransform: "uppercase", opacity: 0.7 }}>{t('AI Solution Designer — sample')}</p>
          <p style={{ margin: "0 0 10px", fontSize: "var(--text-lg)", lineHeight: 1.6 }}>{t('"Who should this agent serve, and does its action need approval first?"')}</p>
          <p style={{ margin: 0, fontSize: "var(--text-sm)", opacity: 0.65 }}>{t('— clarifying question, asked automatically before any workflow is generated.')}</p>
          <p style={{ margin: "18px 0 0", paddingTop: "var(--space-3)", borderTop: "1px solid color-mix(in srgb, #f2f2f3 22%, transparent)", fontSize: "var(--text-sm)", opacity: 0.75 }}>{t('Every agent goes through the same gates: spec, workflow, sandbox, evaluation, approval.')}</p>
        </div>
      </section>

      {/* ── RIGHT PANEL — form in blueprint frame ── */}
      <section style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "var(--space-10)" }}>
        <div className="blueprint" style={{ width: "100%", maxWidth: 392, padding: "var(--space-8)", animation: "agUp .5s .08s ease both" }}>
          <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
          <h2 style={{ margin: "0 0 4px", fontSize: "var(--text-2xl)" }}>{t(title)}</h2>
          <p style={{ margin: "0 0 22px", fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 60%, transparent)" }}>{t(subtitle)}</p>
          {children}
          {footer}
        </div>
      </section>
    </main>
  );
}
