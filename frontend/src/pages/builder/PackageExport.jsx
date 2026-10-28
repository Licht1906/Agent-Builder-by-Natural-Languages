import { useEffect, useState } from "react";
import { PackageCheck } from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Spinner } from "../../components/ui";
import { useLang } from "../../i18n";

/* ============================================================
   "Xuất gói chạy độc lập" (DS-19 / ADR-0059 §10): this version as a zip a
   company runs inside its own network — runtime.yaml, access.yaml, the
   bundle, its documents, a compose file and a README.

   Locked until the version is approved: what leaves the platform to run
   unattended is what an admin approved, and the server refuses the same.
   "Kèm images cho air-gap" is offered only once an admin has provided the
   signed images (`PACKAGE_IMAGES_PATH`); otherwise it says so, greyed out.
   ============================================================ */
export default function PackageExport({ agentId, versionId, versionStatus, notify }) {
  const { t } = useLang();
  const [options, setOptions] = useState(null);
  const [airgap, setAirgap] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    setOptions(null);
    setAirgap(false);
    api.agents
      .packageOptions(agentId, versionId)
      .then((found) => live && setOptions(found))
      .catch((err) => live && setError(err.message));
    return () => { live = false; };
  }, [agentId, versionId, versionStatus]);

  const approved = Boolean(options?.approved);
  const airgapAvailable = Boolean(options?.airgap_available);

  async function download() {
    setBusy(true);
    setError("");
    try {
      const { blob, filename } = await api.agents.exportPackage(agentId, versionId, airgap && airgapAvailable);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || `agent-${agentId}-v${versionId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      notify?.(t("Package downloaded"));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="blueprint" style={{ padding: "var(--space-5)" }} data-testid="package-export">
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <h2 style={{ margin: "0 0 6px", fontSize: "var(--text-2xl)" }}>{t("Export a standalone package")}</h2>
      <p className="text-muted" style={{ margin: "0 0 12px", fontSize: "var(--text-sm)", lineHeight: 1.6 }}>
        {t("A zip the company runs on its own servers: the agent, its access rules, its documents, a docker-compose file and a README. No password, DSN or token is ever in it — IT fills those in.")}
      </p>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "var(--text-sm)", opacity: airgapAvailable ? 1 : 0.6 }}>
        <input
          type="checkbox"
          checked={airgap && airgapAvailable}
          disabled={!airgapAvailable || !approved || busy}
          onChange={(event) => setAirgap(event.target.checked)}
        />
        {t("Include images for air-gap")}
      </label>
      {options && !airgapAvailable && (
        <p className="text-muted" style={{ margin: "4px 0 0 24px", fontSize: "var(--text-xs)" }}>
          {t("No signed images on this platform yet — an admin runs scripts/package_runtime_images.sh and sets PACKAGE_IMAGES_PATH.")}
        </p>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 12, flexWrap: "wrap" }}>
        <button className="btn btn-primary" onClick={download} disabled={!approved || busy} style={{ height: 36 }}>
          {busy ? <Spinner size={13} /> : <PackageCheck size={14} />}{t("Export standalone package")}
        </button>
        {options && !approved && (
          <span className="text-muted" style={{ fontSize: "var(--text-xs)" }}>{t("Only an approved version can be exported as a package.")}</span>
        )}
      </div>
      <ErrorNote>{error}</ErrorNote>
    </div>
  );
}
