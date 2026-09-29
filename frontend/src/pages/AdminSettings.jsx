import { useCallback, useEffect, useState } from "react";
import { CloudDownload, Save } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Hint, PageHead, PageShell, Spinner } from "../components/ui";
import { useLang } from "../i18n";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   The platform configuration screen (#12).

   Two settings, and deliberately only two — they are the two `GET /admin/config`
   serves and `PATCH /admin/config` accepts. A toggle with nothing behind it
   reads as a setting that has been applied, which is worse on a governance
   screen than a setting that is missing.

   Each is saved on its own call, with the other field omitted rather than sent
   back unchanged: the endpoint reads an omitted field as "leave this one alone",
   which is what stops an admin editing the threshold from also writing back an
   allowlist they read before somebody else changed it.

   The threshold is stored as a fraction and edited here as a percentage,
   because "80% of cases must pass" is the sentence an admin is actually
   deciding and `0.8` is the sentence the database keeps. The conversion lives
   at this boundary and nowhere else: a percentage reaching the API, or a
   fraction reaching a number input, is the same bug seen from two ends.
   ============================================================ */

/** The stored fraction as a percentage an admin reads, rounded to whole points.
 *
 *  Whole points because the field steps in them: showing `79.99999999999999`
 *  for a threshold saved as 80 is what unrounded float arithmetic does to a
 *  round trip, and an admin re-reading their own saved value is exactly when
 *  they would see it. */
const asPercent = (fraction) => Math.round(fraction * 1000) / 10;

/** A percentage back to the fraction the platform stores. */
const asFraction = (percent) => Math.round(percent * 10) / 1000;

const sameSet = (left, right) =>
  left.length === right.length && [...left].sort().join("|") === [...right].sort().join("|");

export default function AdminSettings({ notify }) {
  const { t } = useLang();
  const [config, setConfig] = useState(null);
  const [percent, setPercent] = useState("");
  const [allowlist, setAllowlist] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const adopt = useCallback((fetched) => {
    setConfig(fetched);
    setPercent(String(asPercent(fetched.eval_pass_threshold)));
    setAllowlist(fetched.model_allowlist || []);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      adopt(await api.platformConfig.read());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [adopt]);

  useEffect(() => { load(); }, [load]);

  const typed = Number(percent);
  const validPercent = percent.trim() !== "" && Number.isFinite(typed) && typed >= 0 && typed <= 100;
  const thresholdChanged = config !== null && validPercent && asFraction(typed) !== config.eval_pass_threshold;
  const allowlistChanged = config !== null && !sameSet(allowlist, config.model_allowlist || []);

  async function run(kind, action, success) {
    setBusy(kind);
    setError("");
    try {
      adopt(await action());
      notify(success);
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  const saveThreshold = () => run(
    "threshold",
    () => api.platformConfig.setEvalThreshold(asFraction(typed)),
    t("Eval threshold is now {n}%", { n: typed })
  );

  const saveAllowlist = () => run(
    "allowlist",
    () => api.platformConfig.setModelAllowlist(allowlist),
    t("Model allowlist updated")
  );

  const toggleModel = (name) =>
    setAllowlist((current) => (current.includes(name) ? current.filter((entry) => entry !== name) : [...current, name]));

  return (
    <PageShell width="narrow">
        <PageHead
          title={t('Platform settings')}
          hint={t("Applies to every agent on this deployment. Changes take effect on the next run, not on runs already in flight.")}
        />

        <ErrorNote>{error}</ErrorNote>

        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}

        {!loading && config && (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {/* ── The eval bar ── */}
            <div className="blueprint" style={{ padding: "var(--space-4)", display: "flex", alignItems: "center", gap: 18, animation: "agUp .35s ease both" }}>
              <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", display: "flex", alignItems: "center", gap: 8 }}>
                  {t('Evaluation pass threshold')}
                  <span className="ab-mono" style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
                    {validPercent ? asFraction(typed) : config.eval_pass_threshold}
                  </span>
                  <Hint text={t("The score a version must reach before it may be submitted for approval. Stored as")} />
                </p>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flex: "none" }}>
                <input
                  className="input"
                  type="number"
                  min={0}
                  max={100}
                  aria-label={t("Evaluation pass threshold, percent")}
                  disabled={busy === "threshold"}
                  value={percent}
                  onChange={(e) => setPercent(e.target.value)}
                  style={{ width: 84, height: 36, textAlign: "right", fontFamily: "ui-monospace, monospace" }}
                />
                <button className="btn btn-primary" disabled={!thresholdChanged || !!busy} onClick={saveThreshold} style={{ height: 36 }}>
                  {busy === "threshold" ? <Spinner size={13} /> : <Save size={14} />}{t("Save")}
                </button>
              </div>
            </div>

            {/* ── What an agent may be built out of ── */}
            <div className="blueprint" style={{ padding: "var(--space-4)", animation: "agUp .35s ease both" }}>
              <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
              <div style={{ display: "flex", alignItems: "flex-start", gap: 18, marginBottom: 14 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{t('Model allowlist')}<Hint text={t("Which of the vetted models an agent's steps may be assigned. The catalog below is fixed by this build — an empty allowlist is refused, because it is not a policy.")} /></p>
                </div>
                <button className="btn btn-primary" disabled={!allowlistChanged || !!busy} onClick={saveAllowlist} style={{ height: 36, flex: "none" }}>
                  {busy === "allowlist" ? <Spinner size={13} /> : <Save size={14} />}{t("Save")}
                </button>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(232px, 1fr))", gap: 8 }}>
                {config.available_models.map((model) => {
                  const on = allowlist.includes(model.name);
                  return (
                    <label
                      key={model.name}
                      style={{
                        display: "flex", alignItems: "center", gap: 10, padding: "var(--space-2) var(--space-3)", cursor: "pointer",
                        border: `1px solid ${on ? "var(--color-accent)" : "var(--color-divider)"}`,
                        background: on ? "color-mix(in srgb, var(--color-accent) 8%, transparent)" : "transparent",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={() => toggleModel(model.name)}
                        style={{ width: 14, height: 14, accentColor: "var(--color-accent)", margin: 0, flex: "none" }}
                      />
                      <span style={{ flex: 1, minWidth: 0, fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", overflow: "hidden", textOverflow: "ellipsis" }}>
                        {model.name}
                      </span>
                      <span className="tag tag-neutral" style={{ flex: "none" }}>{model.tier}</span>
                    </label>
                  );
                })}
              </div>
            </div>

            <p style={{ margin: 0, fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>
{t("last changed")} {new Date(config.updated_at).toLocaleString()}
            </p>
          </div>
        )}

        <ControlPlanePull notify={notify} />
      </PageShell>
  );
}

/* UC1-26 (#142) — pulling a signed template from the vendor's control plane.
   Lives beside the eval bar because both are platform-wide switches nobody
   but an admin should touch, and neither belongs to any one agent.

   Reachable but inert on a fresh install: `CONTROL_PLANE_URL` ships empty on
   purpose, and the endpoint answers accordingly. That refusal is worth showing
   rather than hiding the control — an admin who cannot find the button has no
   way to learn the reason it would not have worked. */
function ControlPlanePull({ notify }) {
  const [templateId, setTemplateId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pulled, setPulled] = useState(null);

  async function pull() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.controlPlane.pullTemplate(templateId.trim());
      setPulled(result);
      notify(`Đã kéo template ${result.template_id} (build ${result.build_id})`);
    } catch (err) {
      setError(err.message);
      setPulled(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ab-card" style={{ padding: "var(--space-5)", marginTop: 16 }}>
      <p className="ab-label" style={{ marginBottom: 8 }}>Control plane — kéo template</p>
      <p style={{ fontSize: "var(--text-xs)", color: "var(--ab-muted)", margin: "0 0 10px", lineHeight: 1.6 }}>
        Chỉ hoạt động khi đã cấu hình <span className="ab-mono">CONTROL_PLANE_URL</span> và{" "}
        <span className="ab-mono">CONTROL_PLANE_PUBLIC_KEY</span>. Chữ ký Ed25519 được kiểm trước khi nội dung được nhận.
      </p>
      <input
        className="ab-input"
        value={templateId}
        placeholder="template id"
        spellCheck={false}
        disabled={busy}
        onChange={(event) => setTemplateId(event.target.value)}
      />
      <ErrorNote>{error}</ErrorNote>
      <button className="ab-btn ab-btn-outline ab-btn-sm" style={{ marginTop: 12 }} onClick={pull} disabled={busy || !templateId.trim()}>
        {busy ? <Spinner size={13} /> : <CloudDownload size={14} />}Kéo về
      </button>

      {pulled && (
        <pre
          className="ab-mono ab-scrollbar"
          style={{ fontSize: "var(--text-2xs)", background: "rgba(127,127,127,.07)", borderRadius: 6, padding: "var(--space-2)", margin: "12px 0 0", overflowX: "auto", maxHeight: 260 }}
        >
          {JSON.stringify(pulled.content, null, 2)}
        </pre>
      )}
    </div>
  );
}
