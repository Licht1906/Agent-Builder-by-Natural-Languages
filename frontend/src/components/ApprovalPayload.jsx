import { useLang } from "../i18n";

/* ============================================================
   The call an approval stands in front of, shown as it will be made
   (DS-08 / ADR-0059 §7).

   Verbatim on purpose: no Markdown, no trimming, no summary. The backend
   hashes exactly these arguments and refuses to send anything that differs
   by a character, so what is drawn here has to be what was hashed — a
   rendered body could look the same as a different one.
   ============================================================ */

//: Labels for the arguments a reader most needs to see; anything else keeps its own name.
const LABELS = { to: "Recipient", cc: "Cc", bcc: "Bcc", subject: "Subject", body: "Body" };

export default function ApprovalPayload({ actions }) {
  const { t } = useLang();
  if (!actions || actions.length === 0) return null;
  return (
    <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
      {actions.map((action) => (
        <div
          key={action.node_id}
          data-testid="approval-payload"
          style={{ border: "1px solid var(--color-divider)", background: "var(--color-surface)", color: "var(--color-text)", padding: "var(--space-2) var(--space-3)" }}
        >
          <p style={{ margin: "0 0 6px", fontSize: "var(--text-xs)" }}>
            {t("Will call")} <span className="ab-mono">{action.tool}</span>
          </p>
          <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 10px", fontSize: "var(--text-sm)" }}>
            {Object.entries(action.args || {}).map(([key, value]) => (
              <div key={key} style={{ display: "contents" }}>
                <dt className="text-muted" style={{ fontSize: "var(--text-xs)", paddingTop: 2 }}>{LABELS[key] ? t(LABELS[key]) : key}</dt>
                <dd style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word", minWidth: 0 }}>
                  {typeof value === "string" ? value : JSON.stringify(value)}
                </dd>
              </div>
            ))}
          </dl>
          <p className="text-muted" style={{ margin: "6px 0 0", fontSize: "var(--text-xs)" }}>
            {t("Exactly this will be sent — anything different, even by one character, is refused.")}
          </p>
        </div>
      ))}
    </div>
  );
}
