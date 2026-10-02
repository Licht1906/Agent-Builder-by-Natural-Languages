import { useEffect, useRef, useState } from "react";
import { Send, Wand2, AlertTriangle, Layers, ArrowUp, MessageSquare } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Spinner } from "../components/ui";
import { AgentMascot, AgentBusy } from "../components/mascot";
import { useAgentActivity, useAgentSide, useAgentStandIn } from "../components/agent-presence";
import { useLang } from "../i18n";

/* ============================================================
   The AI Solution Designer interview (#42) — and the workspace's home.

   Signed in, this is the first screen (App.jsx lands here), so it opens as a
   doorway rather than a form: the mascot watching the cursor, one greeting, one
   input, three example tasks — the shape every prompt-first product opens with
   (Perplexity, Manus, WRITER). The moment the first description is sent, the
   same screen becomes the interview it always was: the description is the
   interview's opening answer, and the conversation continues from it. Nothing
   is handed off between two pages — there is no second page.
   ============================================================ */

/** Example tasks, phrased the way a creator would actually type them.
 *
 *  Two strings each, and the split is the point. The chip carries a short
 *  *name* so that three of them fit on one row; clicking fills the input with
 *  the long form, which is what actually teaches the shape of a good
 *  description. Printing the long form on the chip made the row wrap to two
 *  lines and turned three examples into a wall of text under the input.
 *
 *  Filling rather than sending, still: the examples teach the shape, and the
 *  person should finish the thought as their own before it becomes the
 *  interview's first answer. */
const SUGGESTIONS = [
  { label: "Draft Gmail replies", text: "Read unanswered Gmail and draft replies for my approval" },
  { label: "Summarize a Drive folder", text: "Summarize new files in a Drive folder every morning" },
  { label: "Triage support tickets", text: "Triage incoming support tickets by urgency" },
];

export default function DesignerPage({ onSpecReady }) {
  const { t } = useLang();
  const [interview, setInterview] = useState(null);
  const [input, setInput] = useState("");
  /* Named, not boolean: "busy" cannot tell the shell's agent whether it is
     reading an answer or drafting a specification, and those are different
     things to be watching. */
  const [job, setJob] = useState("");
  const busy = job !== "";
  const [error, setError] = useState("");
  /* Claude-Code-style recommend for the open question: `sug` is `{field, multi,
     suggestions}` or null, `picked` the chips chosen so far on a list field.
     Both are the open question's — cleared and refetched the moment it changes,
     and an empty `suggestions` just leaves the plain input, same as today. */
  const [sug, setSug] = useState(null);
  const [sugBusy, setSugBusy] = useState(false);
  const [picked, setPicked] = useState([]);
  const endRef = useRef(null);
  const heroInputRef = useRef(null);

  /* The composer is one line until it needs more. Reset to `auto` first or
     scrollHeight only ever reports the height it already has, and the box
     grows on every keystroke and never shrinks back. The CSS max-height then
     hands it a scrollbar rather than letting it eat the page. */
  const grow = (el) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  };

  useEffect(() => {
    if (interview) endRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [interview, busy]);

  /* Fetch pickable answers whenever a new question is on screen. Keyed on the
     question text, so it refetches when the interview moves on and does nothing
     when the interview is finished. A failed call is swallowed to null — the
     screen falls back to the plain input, which is where it started. */
  useEffect(() => {
    const question = interview?.open_question;
    if (!question) {
      setSug(null);
      setPicked([]);
      return;
    }
    let alive = true;
    setSug(null);
    setPicked([]);
    setSugBusy(true);
    api.designer
      .suggestions(interview.id)
      .then((result) => alive && setSug(result))
      .catch(() => alive && setSug(null))
      .finally(() => alive && setSugBusy(false));
    return () => {
      alive = false;
    };
  }, [interview?.id, interview?.open_question?.text]);

  async function submit() {
    const typed = input.trim();
    const first = !interview;
    /* Picked chips only carry an answer on a list field — on a scalar one a
       click fills the input instead, so `picked` stays empty there. The answer
       sent is the chips plus whatever was typed, one per line; the designer
       reads the lot back into the fields exactly as it reads a hand-typed one. */
    const chosen = !first && sug?.multi ? picked : [];
    const outgoing = first ? typed : [...chosen, typed].filter(Boolean).join("\n");
    if (!outgoing || busy) return;
    setJob("reading");
    setError("");
    /* An interview answer clears at once, the way every chat does. The first
       description stays put in the disabled input while it is being read — the
       conversation it will appear in does not exist yet, so clearing here would
       make the words vanish from the screen entirely for a few seconds. */
    if (!first) {
      setInput("");
      setPicked([]);
    }
    try {
      setInterview(first ? await api.designer.start(outgoing) : await api.designer.answer(interview.id, outgoing));
      if (first) setInput("");
    } catch (err) {
      setError(err.message);
      setInput(typed);
      if (!first) setPicked(chosen);
    } finally {
      setJob("");
    }
  }

  async function buildSpec() {
    setJob("spec");
    setError("");
    try {
      const built = await api.designer.buildSpec(interview.id);
      onSpecReady(built);
    } catch (err) {
      setError(err.message);
    } finally {
      setJob("");
    }
  }

  useAgentActivity("thinking", job === "reading", interview ? t("reading your answer") : t("reading your description"));
  useAgentActivity("builder", job === "spec", t("drafting the specification"));
  /* The home stage *is* the agent: 190px of it in the middle of the page,
     watching the cursor, with its state on its face. The corner dock would be a
     second copy of the same creature standing two feet away from the first. */
  useAgentStandIn(!interview);
  /* Both stages of this screen carry a composer — the home stage's is the
     first thing anyone sees — so the side is declared for both, not only for
     the interview. It was conditional on `interview` at first, on the argument
     that the home stage stands the dock down anyway so the side cannot matter.
     It does matter, twice over: the declaration is about *the screen*, and the
     screen has a chat bar either way; and the moment the first message is sent
     the stand-in ends, so a side declared only from that point would have the
     agent arrive in the wrong corner and slide across. */
  useAgentSide("left");

  /* ── Home stage: nothing sent yet. ──
     Also where the first send waits: the mascot switches from welcoming to
     thinking on this same stage, and the interview replaces it only when the
     first question has actually arrived — the person watches one screen think
     instead of being cut to an empty conversation. */
  if (!interview) {
    return (
      <main style={{ flex: 1, minHeight: 0, position: "relative", overflow: "hidden", display: "flex", background: "var(--color-bg)", color: "var(--color-text)" }}>
        {/* Ground for the home stage. The screen used to be a flat slab with a
            robot on it; the agent is drawn standing on a blueprint, so the page
            it opens on is that blueprint — the same ruled grid the landing page
            and the sign-in panel drift, at the same 46px pitch, so the three
            entrances to the product are recognisably one place. Behind the
            mascot, a soft wash of the brand blue and cyan, which is what stops
            the composition reading as hard-edged. Both are decoration and both
            are inert: outside the scroll container, so they hold still while
            the conversation moves, and out of the pointer's way. */}
        <div aria-hidden="true" style={{ position: "absolute", inset: 0, zIndex: 0, opacity: 0.06, backgroundImage: "linear-gradient(var(--color-text) 1px, transparent 1px), linear-gradient(90deg, var(--color-text) 1px, transparent 1px)", backgroundSize: "46px 46px", animation: "agDrift 14s linear infinite", pointerEvents: "none" }} />
        <div aria-hidden="true" style={{ position: "absolute", inset: 0, zIndex: 0, pointerEvents: "none", background: "radial-gradient(58% 44% at 50% 26%, color-mix(in srgb, var(--brand-blue) 13%, transparent), transparent 70%), radial-gradient(32% 26% at 50% 20%, color-mix(in srgb, var(--brand-cyan) 12%, transparent), transparent 72%)" }} />

        <div className="ab-scrollbar" style={{ position: "relative", zIndex: 1, flex: 1, minWidth: 0, overflowY: "auto", display: "flex" }}>
        <div style={{ margin: "auto", width: "min(680px, calc(100% - 48px))", display: "flex", flexDirection: "column", alignItems: "center", padding: "var(--space-12) 0", animation: "agUp .5s ease both" }}>

          <AgentMascot state={busy ? "thinking" : "welcome"} size={190} follow={!busy} />

          {/* The heading, one input, one row of examples — and nothing else.
              A paragraph explaining that the designer interviews you and drafts
              a specification used to stand here: three lines of instructions
              above the box, read once and then scrolled past forever, on the
              screen a person sees more often than any other. The examples under
              the input teach the same thing by being examples. */}
          <h1 className="ab-display" style={{ margin: "var(--space-2) 0 var(--space-5)", textAlign: "center" }}>
            {t("What are we building today?")}
          </h1>

          {/* ── The composer ──
              It used to be the canvas's own `.ab-chatbar` — a row, with the icon
              pinned to the top-left and the send button to the bottom-right of a
              two-row textarea, and a void between them. Then it was two rows
              inside one border, which fixed the void and spent a whole line of
              height on a mark and a button that sit beside the text perfectly
              well — an empty rule across the box whenever the text is short,
              which is almost always.

              So: one row. Mark, what you are typing, send. The box is one line
              tall until there is a second line to hold, and it grows from
              there; `:focus-within` on the border, not height, is what says
              this is the live instrument. */}
          <form className="ab-compose" onSubmit={(e) => { e.preventDefault(); submit(); }}>
            <span className="ab-compose-mark" aria-hidden="true"><MessageSquare size={15} /></span>
            <textarea
              ref={heroInputRef}
              rows={1}
              autoFocus
              aria-label={t("Describe the agent")}
              placeholder={t("Describe the agent — who it serves, what it reads, what it must do…")}
              value={input}
              disabled={busy}
              onChange={(e) => { setInput(e.target.value); grow(e.target); }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
              }}
            />
            <button
              type="submit"
              className="btn btn-primary btn-icon"
              disabled={busy || !input.trim()}
              aria-label={t("Send")}
              title={t("Send")}
              style={{ width: 30, height: 30, flexShrink: 0 }}
            >
              {busy ? <Spinner size={14} /> : <ArrowUp size={16} />}
            </button>
          </form>

          {busy ? (
            <div style={{ marginTop: "var(--space-3)" }}>
              <AgentBusy label={t("reading your description")} size={34} />
            </div>
          ) : (
            /* One row, never two. nowrap plus a scroller rather than wrap:
               a longer translation slides instead of pushing the composer up
               the page, so the screen is the same height in every language. */
            <div className="ab-scrollbar" style={{ display: "flex", flexWrap: "nowrap", overflowX: "auto", gap: "var(--space-2)", justifyContent: "center", marginTop: "var(--space-3)", maxWidth: "100%" }}>
              {SUGGESTIONS.map((entry) => (
                <button
                  key={entry.label}
                  type="button"
                  className="btn btn-secondary"
                  title={t(entry.text)}
                  style={{ height: 30, flex: "none", paddingInline: "var(--space-3)", fontSize: "var(--text-xs)" }}
                  onClick={() => {
                    /* Fill, then hand focus back to the input: the next
                       keystroke — or Enter — belongs to the description, not to
                       the chip that seeded it. */
                    setInput(t(entry.text));
                    heroInputRef.current?.focus();
                    /* After the value lands, not before: the scrollHeight of a
                       box that still holds the old text measures the old text. */
                    requestAnimationFrame(() => grow(heroInputRef.current));
                  }}
                >
                  {t(entry.label)}
                </button>
              ))}
            </div>
          )}

          <div style={{ marginTop: 14, width: "100%" }}><ErrorNote>{error}</ErrorNote></div>
        </div>
        </div>
      </main>
    );
  }

  /* ── Interview stage: the conversation the description started. ── */
  const step = interview.answered;
  const total = interview.total;
  const pct = Math.round((step / total) * 100);

  return (
    <main style={{ flex: 1, minHeight: 0, position: "relative", display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--color-bg)", color: "var(--color-text)" }}>
      {/* The same ground as the home stage, fainter. Sending the first message
          does not move you to another page — it turns this one into the
          conversation — so the floor must not drop out from under it. The glow
          does not come along: it was anchored to a mascot that is no longer
          standing there, and a wash behind a scrolling transcript is noise. */}
      <div aria-hidden="true" style={{ position: "absolute", inset: 0, zIndex: 0, opacity: 0.035, backgroundImage: "linear-gradient(var(--color-text) 1px, transparent 1px), linear-gradient(90deg, var(--color-text) 1px, transparent 1px)", backgroundSize: "46px 46px", animation: "agDrift 22s linear infinite", pointerEvents: "none" }} />

      {/* ── The interview's own bar ──
          It used to open with "AI Solution Designer" in heading type, which is
          the very thing the shell header an inch above it already says. What
          this bar is for is how far through the interview you are and the one
          decision waiting at the end of it, so that is all it carries now —
          and it costs 26px less height for saying more. */}
      <div className="ab-toolbar" style={{ position: "relative", zIndex: 1, padding: "var(--space-2) var(--page-pad-x)", borderBottom: "1px solid var(--color-divider)", flexShrink: 0, flexWrap: "nowrap" }}>
        <Wand2 size={15} style={{ flexShrink: 0, color: "var(--color-accent)" }} aria-hidden="true" />
        <span style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 62%, transparent)", whiteSpace: "nowrap" }}>
          {t("{done} of {total} fields answered", { done: step, total })}
        </span>
        <div
          role="progressbar"
          aria-valuenow={step}
          aria-valuemin={0}
          aria-valuemax={total}
          style={{ flex: "1 1 auto", minWidth: 80, maxWidth: 260, height: 4, background: "color-mix(in srgb, var(--color-text) 10%, transparent)", overflow: "hidden" }}
        >
          <div style={{ height: "100%", background: "var(--color-accent)", transition: "width .45s cubic-bezier(.4,0,.2,1)", width: `${pct}%` }} />
        </div>
        {interview.complete && (
          <button type="button" onClick={buildSpec} className="btn btn-primary btn-sm" disabled={busy} style={{ flexShrink: 0, marginLeft: "auto" }}>
            {busy ? <Spinner size={13} /> : <Layers size={13} />}{t("Build the specification")}
          </button>
        )}
      </div>

      {/* ── CHAT MESSAGES CONTAINER ── */}
      <div className="ab-scrollbar" style={{ position: "relative", zIndex: 1, flex: 1, minHeight: 0, overflowY: "auto", padding: "var(--space-6)", display: "flex", flexDirection: "column", gap: 12, maxWidth: 820, width: "100%", margin: "0 auto" }}>
        <div style={{ flex: "1 0 auto" }} />

        <p style={{ margin: 0, alignSelf: "flex-end", maxWidth: "78%", padding: "var(--space-3) var(--space-4)", fontSize: "var(--text-md)", lineHeight: 1.6, whiteSpace: "pre-wrap", background: "var(--color-accent)", color: "var(--color-bg)" }}>
          {interview.description}
        </p>

        {interview.turns.map((turn, index) => (
          <div key={index} style={{ display: "flex", flexDirection: "column", gap: 12, animation: "agUp .3s ease both" }}>
            {turn.question && (
              <p style={{ margin: 0, alignSelf: "flex-start", maxWidth: "82%", padding: "var(--space-3) var(--space-4)", fontSize: "var(--text-md)", lineHeight: 1.6, whiteSpace: "pre-wrap", border: "1px solid var(--color-divider)" }}>
                {turn.question}
              </p>
            )}
            {turn.answer && (
              <p style={{ margin: 0, alignSelf: "flex-end", maxWidth: "78%", padding: "var(--space-3) var(--space-4)", fontSize: "var(--text-md)", lineHeight: 1.6, whiteSpace: "pre-wrap", background: "var(--color-accent)", color: "var(--color-bg)" }}>
                {turn.answer}
              </p>
            )}
          </div>
        ))}

        {interview.open_question && (
          <p style={{ margin: 0, alignSelf: "flex-start", maxWidth: "82%", padding: "var(--space-3) var(--space-4)", fontSize: "var(--text-md)", lineHeight: 1.6, whiteSpace: "pre-wrap", border: "1px solid var(--color-divider)", animation: "agUp .3s ease both" }}>
            {interview.open_question.text}
          </p>
        )}

        {/* ── Pickable answers for the open question — the Claude-Code recommend ──
            Chips under the question: click one to answer (a list field keeps
            adding, a scalar one fills the input to edit). The first may be
            flagged "recommended". No suggestions, or a call that failed, and
            this block simply is not there — the input below is the whole
            interface, unchanged. */}
        {interview.open_question && (sugBusy || sug?.suggestions?.length > 0) && (
          <div style={{ alignSelf: "flex-start", maxWidth: "82%", display: "flex", flexWrap: "wrap", gap: "var(--space-2)", animation: "agUp .3s ease both" }}>
            {sugBusy ? (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
                <Spinner size={13} />{t("Thinking of answers…")}
              </span>
            ) : (
              sug.suggestions.map((s) => {
                const on = sug.multi && picked.includes(s.text);
                return (
                  <button
                    key={s.text}
                    type="button"
                    className={`btn ${on ? "btn-primary" : "btn-secondary"} btn-sm`}
                    style={{ height: "auto", minHeight: 30, paddingBlock: 4, textAlign: "left", whiteSpace: "normal", maxWidth: "100%" }}
                    onClick={() => {
                      if (sug.multi) {
                        setPicked((p) => (p.includes(s.text) ? p.filter((x) => x !== s.text) : [...p, s.text]));
                      } else {
                        setInput(s.text);
                      }
                    }}
                  >
                    {s.recommended && (
                      <span style={{ fontSize: "var(--text-xs)", opacity: 0.7, marginRight: 6, whiteSpace: "nowrap" }}>
                        ★ {t("Recommended")}
                      </span>
                    )}
                    {s.text}
                  </button>
                );
              })
            )}
          </div>
        )}

        {interview.open_question && !sugBusy && sug?.multi && sug?.suggestions?.length > 0 && (
          <p style={{ alignSelf: "flex-start", margin: "-4px 0 0", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>
            {t("Pick any that fit — or type your own below.")}
          </p>
        )}

        {interview.conflicts && interview.conflicts.length > 0 && (
          <div style={{ border: "1px solid var(--color-accent)", background: "var(--color-accent-100)", color: "var(--color-accent-800)", padding: "var(--space-3) var(--space-4)", animation: "agUp .35s ease both" }}>
            <p style={{ margin: "0 0 6px", fontFamily: "var(--font-heading)", fontSize: "var(--text-lg)", display: "flex", alignItems: "center", gap: 6 }}>
              <AlertTriangle size={15} />{t("Still contradictory")}
            </p>
            {interview.conflicts.map((conflict, index) => (
              <p key={index} style={{ margin: "3px 0", fontSize: "var(--text-sm)", lineHeight: 1.55 }}>· {conflict}</p>
            ))}
          </div>
        )}

        {interview.complete && (
          <div style={{ border: "1px solid var(--color-accent)", padding: "var(--space-3) var(--space-4)", fontSize: "var(--text-md)", lineHeight: 1.6, animation: "agUp .35s ease both" }}>
            {t("All {total} fields are answered and nothing is left contradictory. Build the specification to review it before any workflow is generated.", { total })}
          </div>
        )}

        {busy && (
          <div style={{ alignSelf: "flex-start", animation: "agIn .2s ease both" }}>
            <AgentBusy label={t("thinking")} />
          </div>
        )}

        <ErrorNote>{error}</ErrorNote>
        <div ref={endRef} />
      </div>

      {/* ── CHAT INPUT FORM ── */}
      <form
        onSubmit={(e) => { e.preventDefault(); submit(); }}
        style={{ position: "relative", zIndex: 1, display: "flex", gap: 10, alignItems: "center", padding: "var(--space-4) var(--space-6)", borderTop: "1px solid var(--color-divider)", maxWidth: 820, width: "100%", margin: "0 auto", flexShrink: 0, boxSizing: "border-box" }}
      >
        <textarea
          className="input"
          rows={1}
          aria-label={t("Answer")}
          placeholder={interview.complete ? t("Interview finished — build spec above") : t("Type your answer…")}
          value={input}
          disabled={busy || (interview.complete && !interview.open_question)}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
          }}
          style={{ flex: 1, minHeight: 44, height: 44, resize: "none", boxSizing: "border-box" }}
        />
        <button type="submit" className="btn btn-primary" disabled={busy || (!input.trim() && picked.length === 0)} style={{ height: 44, paddingInline: "var(--space-5)", flexShrink: 0 }}>
          <Send size={15} />{t("Send")}
        </button>
      </form>
    </main>
  );
}
