import { useEffect } from "react";
import { AlertTriangle, X } from "lucide-react";
import { Spinner } from "./ui";
import { useLang } from "../i18n";

/* A modal for the one kind of action worth stopping somebody on: the kind that
   cannot be undone. It says what will be lost rather than asking "are you sure",
   because "are you sure" is a question nobody has enough information to answer.

   Escape closes it and the backdrop closes it — a dialog you can only leave
   through one of two buttons is a dialog people click through. */
export default function ConfirmDialog({ open, title, body, confirmLabel, busy, onConfirm, onCancel }) {
  const { t } = useLang();
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => event.key === "Escape" && !busy && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;

  return (
    <div className="ab-dialog-backdrop" onClick={() => !busy && onCancel()}>
      <div
        className="ab-dialog"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <div style={{ width: 34, height: 34, flexShrink: 0, display: "grid", placeItems: "center", background: "rgba(239,68,68,.1)", border: "1px solid rgba(239,68,68,.3)" }}>
            <AlertTriangle size={16} style={{ color: "#b91c1c" }} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", margin: 0 }}>{title}</h2>
            <div style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 70%, transparent)", lineHeight: 1.6, marginTop: 8 }}>{body}</div>
          </div>
          <button className="btn btn-ghost btn-icon" style={{ width: 26, height: 26, padding: "var(--space-1)" }} onClick={onCancel} disabled={busy} aria-label={t("Close")}>
            <X size={14} />
          </button>
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 8 }}>
          <button className="btn btn-secondary" onClick={onCancel} disabled={busy}>{t('Cancel')}</button>
          <button className="btn btn-danger" onClick={onConfirm} disabled={busy}>
            {busy ? <Spinner size={13} /> : null}{confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
