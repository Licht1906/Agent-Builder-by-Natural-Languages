import { useEffect, useRef, useState } from "react";
import { MessageSquare, Send, Undo2, ChevronDown, ChevronUp } from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Spinner } from "../../components/ui";
import { AgentBusy } from "../../components/mascot";
import { diffGraphs, describeDiff } from "../../lib/graph-diff";
import { usePaneSize, PaneResizer } from "../../lib/panes";
import { useLang } from "../../i18n";

/* ============================================================
   Say what to change; watch the canvas change.

   The class of edit a canvas is worst at is the one with a shape: inserting an
   approval step is one sentence and four gestures. So this is not a rival
   editor — it produces the same `WorkflowJSON` the palette and the drag handles
   produce, hands it up through the same `onApply`, and the same debounced save
   stores it. Which is also why undo needs nothing special: putting the previous
   graph back is an ordinary edit.

   Drawn as a command bar floating bottom-centre over the canvas (the parent
   positions it) rather than a strip below it: a collapsed row under the
   problems list is a feature people never found, and the graph paid for it in
   height anyway. The transcript opens as its own card above the bar.

   Nothing here is streamed. The server answers with one whole document, because
   a half-applied graph is not a state the canvas can draw.
   ============================================================ */
export default function GraphChat({ agentId, versionId, workflow, readOnly, onApply, notify }) {
  const { t } = useLang();
  const [turns, setTurns] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(true);
  const [focused, setFocused] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef(null);
  const [chatH, resizeChat, resetChat] = usePaneSize("builder.chat", 220, 120, 520);

  const hasBody = turns.length > 0 || busy || Boolean(error);
  /* The examples show once, on focus, before anything has been asked — the
     moment someone is deciding whether this box is worth typing into. */
  const showCard = (open && hasBody) || (focused && !hasBody);

  useEffect(() => {
    if (showCard) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy, showCard]);

  /* The graph as it stands when the request goes out, kept per turn so undo puts
     back what *this* turn replaced rather than whatever is current when the
     button is finally pressed.

     ADR-0050: one turn of the meta-agent loop. The reply narrates itself —
     `cac_buoc` is the loop's own step trail, `loi_nhan` says what it did and
     why, `cau_hoi` is a question back — and may or may not carry a proposal. */
  async function send() {
    const asked = input.trim();
    if (!asked || busy) return;
    const before = workflow;
    setInput("");
    setOpen(true);
    setBusy(true);
    setError("");
    try {
      const reply = await api.workflow.workbenchTurn(agentId, versionId, asked);
      const after = reply.workflow;
      const diff = after ? diffGraphs(before, after) : null;
      setTurns((current) => [
        ...current,
        {
          asked,
          before,
          after,
          diff,
          loiNhan: reply.loi_nhan,
          cauHoi: reply.cau_hoi,
          steps: reply.cac_buoc || [],
          undone: false,
        },
      ]);
      if (diff && !diff.empty) onApply(after, diff.touchedNodeIds);
    } catch (err) {
      setError(err.message);
      setTurns((current) => [...current, { asked, failed: err.message }]);
    } finally {
      setBusy(false);
    }
  }

  function undo(index) {
    const turn = turns[index];
    onApply(turn.before, new Set());
    setTurns((current) => current.map((entry, at) => (at === index ? { ...entry, undone: true } : entry)));
    notify(t("Undone"));
  }

  if (readOnly) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {showCard && hasBody && (
        <PaneResizer
          size={chatH}
          onResize={resizeChat}
          onReset={resetChat}
          axis="y"
          grow={-1}
          label={t("Drag to resize the chat — double-click to reset")}
        />
      )}
      {showCard && (
        <div className="ab-chatlog ab-scrollbar" style={{ maxHeight: chatH }}>

          {turns.map((turn, index) => (
            <div key={index} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div className="ab-chat-bubble-user" style={{ alignSelf: "flex-end", fontSize: "var(--text-sm)" }}>
                {turn.asked}
              </div>
              <div
                className="ab-chat-bubble-agent"
                style={{
                  fontSize: "var(--text-sm)",
                  ...(turn.failed ? { borderColor: "rgba(239,68,68,.3)", color: "#f8a5a5" } : {}),
                  ...(turn.undone ? { opacity: 0.5 } : {}),
                }}
              >
                {turn.failed ? (
                  turn.failed
                ) : (
                  <>
                    {/* The meta agent's own step trail — the loop made visible. */}
                    {(turn.steps || []).map((step, at) => (
                      <span
                        key={`s${at}`}
                        style={{ display: "block", fontSize: "var(--text-xs)", color: step.ok === false ? "var(--ab-warn)" : "var(--ab-muted)" }}
                      >
                        ⚙ {step.ke}
                      </span>
                    ))}
                    {turn.loiNhan && <span style={{ display: "block", marginTop: turn.steps?.length ? 5 : 0 }}>{turn.loiNhan}</span>}
                    {turn.diff &&
                      describeDiff(turn.diff).map((line, at) => (
                        <span key={at} style={{ display: "block", fontSize: "var(--text-xs)", color: "var(--ab-muted)" }}>
                          · {line}
                        </span>
                      ))}
                    {turn.cauHoi && (
                      <span style={{ display: "block", marginTop: 5, color: "var(--ab-primary)", fontWeight: 500 }}>
                        {turn.cauHoi}
                      </span>
                    )}
                    {turn.diff && !turn.diff.empty && (
                      <button
                        className="ab-btn ab-btn-ghost ab-btn-sm"
                        style={{ marginTop: 6, padding: "var(--space-1) var(--space-2)" }}
                        onClick={() => undo(index)}
                        disabled={turn.undone}
                      >
                        <Undo2 size={12} />
                        {turn.undone ? t("Undone") : t("Undo")}
                      </button>
                    )}
                  </>
                )}
              </div>
            </div>
          ))}

          {busy && (
            <div className="ab-chat-bubble-agent">
              <AgentBusy state="builder" label={t("editing the workflow")} size={30} />
            </div>
          )}
          <ErrorNote>{error}</ErrorNote>
          <div ref={endRef} />
        </div>
      )}

      <div className="ab-chatbar">
        <MessageSquare size={15} style={{ color: "var(--ab-primary)", flexShrink: 0 }} />
        <input
          placeholder={t("For example: “add a human approval step before sending mail”, “drop the web search step”. Files and connections are picked in the Inspector — chat does not fill those in.")}
          value={input}
          disabled={busy}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && send()}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
        />
        {turns.length > 0 && (
          <button
            className="ab-btn ab-btn-ghost ab-btn-sm"
            style={{ padding: "var(--space-1) var(--space-2)", gap: 5 }}
            onClick={() => setOpen((shown) => !shown)}
            title={t("Edit by chat")}
          >
            {turns.length}
            {open ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          </button>
        )}
        <button className="ab-btn ab-btn-primary ab-btn-icon" style={{ width: 32, height: 32 }} onClick={send} disabled={busy || !input.trim()}>
          {busy ? <Spinner size={14} /> : <Send size={14} />}
        </button>
      </div>
    </div>
  );
}
