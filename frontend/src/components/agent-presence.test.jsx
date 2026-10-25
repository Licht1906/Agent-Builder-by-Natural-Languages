import { useState } from "react";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, afterEach } from "vitest";
import { LanguageProvider } from "../i18n";
import {
  AgentPresenceProvider, AgentPresence, useAgentActivity, useAgentBeat, elapsedLabel,
  useAgentStandIn, useAgentPose, useAgentSide,
} from "./agent-presence";

/* The point of this module is that presence is *continuous* and *single*: one
 * agent, mounted once, that survives whatever the screens below it do. So what
 * is worth pinning down is the arbitration — which of several simultaneous
 * activities wins, and whether an activity can outlive the screen that
 * declared it — plus the two promises the corner dock makes about behaving
 * itself: it never intercepts a click, and it never announces twice. */

const shell = (ui) => render(
  <LanguageProvider>
    <AgentPresenceProvider>
      <AgentPresence />
      {ui}
    </AgentPresenceProvider>
  </LanguageProvider>
);

const stateOf = () => [...document.querySelectorAll(".al-mascot")]
  .map((node) => node.className.match(/al-mascot--(\w+)/)?.[1]);

const dock = () => document.querySelector("[data-agent-presence]");
/** What the corner actually says out loud, drawn. */
const plate = () => document.querySelector(".ag-dock__plate")?.textContent ?? null;
/** The other things running, listed above it. */
const alsoRunning = () => [...document.querySelectorAll(".ag-dock__also")].map((node) => node.textContent);
/** The one live region — what a screen reader is told. */
const announced = () => document.querySelector("[role='status']").textContent;

function Doing({ state, label, active = true }) {
  useAgentActivity(state, active, label);
  return null;
}

afterEach(() => { vi.useRealTimers(); });

describe("one agent, whatever is happening", () => {
  it("idles when nothing is running, and stays on screen doing it", () => {
    shell(null);
    /* Not hidden — the whole idea is that it is the same creature the whole
       time, so "nothing to do" is a pose, not an absence. */
    expect(stateOf()).toEqual(["idle"]);
    expect(dock()).toHaveAttribute("data-busy", "0");
  });

  it("shows the running operation, and its words", () => {
    shell(<Doing state="builder" label="composing the workflow" />);

    expect(stateOf()).toEqual(["builder"]);
    expect(plate()).toMatch(/composing the workflow/);
    expect(dock()).toHaveAttribute("data-busy", "1");
  });

  it("never draws two agents when two things run at once — the more urgent wins", () => {
    shell(
      <>
        <Doing state="processing" label="running in the sandbox" />
        <Doing state="alert" label="waiting on your approval" />
      </>
    );

    expect(stateOf()).toEqual(["alert"]);
    expect(plate()).toMatch(/waiting on your approval/);
  });

  it("still says the losing operation is running, rather than swallowing it", () => {
    /* One creature, one pose — but two operations. The header slot could only
       ever show the winner, so the other one silently stopped existing; a run
       you started and can no longer see is the failure this prevents. */
    shell(
      <>
        <Doing state="processing" label="running in the sandbox" />
        <Doing state="alert" label="waiting on your approval" />
      </>
    );

    expect(alsoRunning()).toEqual(["running in the sandbox"]);
    expect(document.querySelector(".ag-dock__count").textContent).toBe("+1");
  });

  it("falls back to the newest when two are equally urgent", () => {
    /* Equal rank means the person just caused the second one; that is the one
       they are waiting on. */
    shell(
      <>
        <Doing state="processing" label="saving the workflow" />
        <Doing state="builder" label="composing the workflow" />
      </>
    );
    expect(plate()).toMatch(/composing the workflow/);
  });

  it("keeps a wordless pose wordless — a page change is not a sentence", () => {
    /* `AppLayout` beats "exploring" with no label on every view change. A plate
       that opened anyway would flash an empty box on every click in the nav,
       and the live region would interrupt a screen reader with nothing. */
    shell(<Doing state="exploring" label={null} />);

    expect(stateOf()).toEqual(["exploring"]);
    expect(dock()).toHaveAttribute("data-busy", "1");
    expect(plate()).toBeNull();
    expect(announced()).toBe("");
  });
});

describe("what assistive tech is told", () => {
  it("has exactly one live region, and it is there before there is news", () => {
    /* Mounted empty rather than rendered when work starts: a live region that
       appears already populated is the classic way to be announced by nobody. */
    shell(null);
    expect(document.querySelectorAll("[aria-live]")).toHaveLength(1);
    expect(announced()).toBe("");
  });

  it("says the sentence once, not twice", () => {
    shell(<Doing state="thinking" label="judging the run" />);

    expect(announced()).toBe("judging the run");
    /* The drawn plate is the same words; if it were also in the accessibility
       tree the sentence would be read out twice. */
    expect(document.querySelector(".ag-dock__plate")).toHaveAttribute("aria-hidden", "true");
  });

  it("goes quiet again when the work ends, rather than announcing idleness", async () => {
    function Screen() {
      const [busy, setBusy] = useState(true);
      useAgentActivity("processing", busy, "running in the sandbox");
      return <button type="button" onClick={() => setBusy(false)}>done</button>;
    }
    const actor = userEvent.setup();
    shell(<Screen />);
    expect(announced()).toBe("running in the sandbox");

    await actor.click(screen.getByRole("button", { name: "done" }));

    expect(announced()).toBe("");
  });
});

describe("the corner behaves itself", () => {
  it("cannot take a click meant for the screen underneath it", () => {
    /* The dock is fixed over the bottom-right corner, and screens put real
       controls there — the Builder's inspector runs the full right edge. It is
       only safe to sit on top of them because nothing here is clickable. */
    shell(<Doing state="processing" label="running in the sandbox" />);
    expect(dock()).toHaveClass("ag-dock");
    expect(dock().querySelector("button")).toBeNull();
    expect(dock().querySelector("a")).toBeNull();
  });

  it("fades out from under the pointer, and comes back", async () => {
    /* jsdom gives every element a zero-sized rect at the origin, so "near the
       dock" is the origin here and "away" is anywhere else. What is being
       tested is the wiring, not the arithmetic. */
    shell(null);

    fireEvent.mouseMove(window, { clientX: 0, clientY: 0 });
    await waitFor(() => expect(dock()).toHaveAttribute("data-ghost", "1"));

    fireEvent.mouseMove(window, { clientX: 900, clientY: 620 });
    await waitFor(() => expect(dock()).toHaveAttribute("data-ghost", "0"));
  });

  it("dozes off when nothing has happened for a long time, and wakes on a keystroke", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    shell(null);
    expect(stateOf()).toEqual(["idle"]);

    await act(async () => { vi.advanceTimersByTime(90_000); });
    await waitFor(() => expect(stateOf()).toEqual(["rest"]));

    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(stateOf()).toEqual(["idle"]));
  });

  it("never dozes off while something is running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    shell(<Doing state="processing" label="running in the sandbox" />);

    await act(async () => { vi.advanceTimersByTime(90_000); });

    expect(stateOf()).toEqual(["processing"]);
  });
});

describe("how long it has been going", () => {
  it("says nothing for the first few seconds, then counts", () => {
    /* A stopwatch on every click is noise. One that turns up when an operation
       is taking a while is the answer to the question you were about to ask. */
    expect(elapsedLabel(0)).toBeNull();
    expect(elapsedLabel(2_400)).toBeNull();
    expect(elapsedLabel(7_000)).toBe("7s");
    expect(elapsedLabel(59_400)).toBe("59s");
    expect(elapsedLabel(83_000)).toBe("1m 23s");
  });

  it("puts the count on the plate once an operation drags", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    shell(<Doing state="processing" label="running in the sandbox" />);
    expect(plate()).not.toMatch(/\ds/);

    await act(async () => { vi.advanceTimersByTime(9_000); });

    await waitFor(() => expect(document.querySelector(".ag-dock__elapsed")).not.toBeNull());
  });
});

describe("activities end when the work does", () => {
  it("returns to idle when the operation finishes", async () => {
    function Screen() {
      const [busy, setBusy] = useState(true);
      useAgentActivity("processing", busy, "running in the sandbox");
      return <button type="button" onClick={() => setBusy(false)}>done</button>;
    }
    const actor = userEvent.setup();
    shell(<Screen />);
    expect(stateOf()).toEqual(["processing"]);

    await actor.click(screen.getByRole("button", { name: "done" }));

    expect(stateOf()).toEqual(["idle"]);
  });

  it("drops an activity whose screen was navigated away from mid-request", () => {
    /* The failure this prevents: leaving a page while it is still fetching used
       to be able to strand the shell's agent working on something nobody is
       waiting for, for the rest of the session. */
    function Host() {
      const [open, setOpen] = useState(true);
      return (
        <>
          {open && <Doing state="thinking" label="judging the run" />}
          <button type="button" onClick={() => setOpen(false)}>leave</button>
        </>
      );
    }
    const { getByRole } = shell(<Host />);
    expect(stateOf()).toEqual(["thinking"]);

    act(() => { getByRole("button", { name: "leave" }).click(); });

    expect(stateOf()).toEqual(["idle"]);
  });
});

describe("the same creature, before and after", () => {
  it("is literally the same element when the screen below it is swapped", () => {
    /* The whole reason presence is inverted. Marking the node by hand is the
       only way to tell "still there" from "rebuilt identically": React would
       give a remounted dock the same markup and a different identity. */
    function Host() {
      const [page, setPage] = useState("one");
      return (
        <>
          {page === "one" ? <Doing state="thinking" label="judging the run" /> : <Doing state="builder" label="composing the workflow" />}
          <button type="button" onClick={() => setPage("two")}>go</button>
        </>
      );
    }
    const { getByRole } = shell(<Host />);
    const before = dock();
    before.dataset.probe = "same-node";
    const mascotBefore = document.querySelector(".al-mascot");

    act(() => { getByRole("button", { name: "go" }).click(); });

    expect(stateOf()).toEqual(["builder"]);
    expect(dock()).toBe(before);
    expect(dock().dataset.probe).toBe("same-node");
    expect(document.querySelector(".al-mascot")).toBe(mascotBefore);
  });
});

describe("beats", () => {
  it("holds a pose for a moment, then lets go", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    function Transition() {
      const beat = useAgentBeat();
      return <button type="button" onClick={() => beat("exploring", null, 500)}>go</button>;
    }
    shell(<Transition />);

    act(() => { screen.getByRole("button", { name: "go" }).click(); });
    expect(stateOf()).toEqual(["exploring"]);

    await act(async () => { vi.advanceTimersByTime(600); });
    await waitFor(() => expect(stateOf()).toEqual(["idle"]));
  });
});

describe("a screen with no shell around it", () => {
  it("declares its activity harmlessly instead of throwing", () => {
    /* Pages are rendered on their own in tests and in the SSR smoke run.
       Declaring work with nobody listening is not an error — it just means
       nothing is showing it. */
    expect(() => render(<Doing state="processing" label="running" />)).not.toThrow();
  });
});

describe("standing down for a screen that is the agent", () => {
  function Embodying({ active = true }) {
    useAgentStandIn(active);
    return null;
  }

  it("fades the corner while a screen draws the agent itself", () => {
    /* The Designer's home stage puts 190px of mascot in the middle of the page.
       A second copy of the same creature in the corner is worse than no corner
       at all: continuity is the point, and two of something is not one thing. */
    shell(<Embodying />);
    expect(dock()).toHaveAttribute("data-standin", "1");
  });

  it("keeps it mounted, so it is still the same element afterwards", () => {
    /* Faded, never unmounted. Unmounting would make the agent a different
       element on the far side of every such screen — exactly the discontinuity
       this whole module was built to remove. */
    const { rerender } = shell(<Embodying />);
    const before = dock();
    expect(before).not.toBeNull();

    rerender(
      <LanguageProvider>
        <AgentPresenceProvider>
          <AgentPresence />
          <Embodying active={false} />
        </AgentPresenceProvider>
      </LanguageProvider>
    );

    expect(dock()).toBe(before);
    expect(dock()).toHaveAttribute("data-standin", "0");
  });

  it("does not narrate from behind a screen that is already saying it", () => {
    /* The home stage prints "reading your description" beside its own mascot.
       The live region repeating it would announce the same sentence twice. */
    shell(
      <>
        <Embodying />
        <Doing state="thinking" label="reading your description" />
      </>
    );
    expect(announced()).toBe("");
  });

  it("comes back the moment no screen is standing in", () => {
    shell(<Embodying active={false} />);
    expect(dock()).toHaveAttribute("data-standin", "0");
  });
});

/* ── A screen's standing pose ────────────────────────────────────────────
   The Canvas tab wants the agent wearing the blueprint the whole time it is
   open. The trap it must not fall into is being declared as an activity: the
   "builder" state ranks 5, so it would sit on top of the thinking and
   exploring states of everything that runs on that tab. A pose is a floor. */

function Posing({ state, active = true }) {
  useAgentPose(state, active);
  return null;
}

describe("a pose a screen holds while it is open", () => {
  it("is what the agent wears when nothing is running", () => {
    shell(<Posing state="builder" />);
    expect(stateOf()).toContain("builder");
  });

  it("does not claim work is happening", () => {
    shell(<Posing state="builder" />);
    expect(dock()).toHaveAttribute("data-busy", "0");
    expect(dock()).toHaveAttribute("data-posed", "1");
    expect(announced()).toBe("");
  });

  it("gives way to real work, then comes back", () => {
    const { rerender } = render(
      <LanguageProvider>
        <AgentPresenceProvider>
          <AgentPresence />
          <Posing state="builder" />
          <Doing state="thinking" label="reading" active />
        </AgentPresenceProvider>
      </LanguageProvider>
    );
    /* The pose must not mask the run — this is the whole reason it is not an
       activity, since "builder" outranks "thinking". */
    expect(stateOf()).toContain("thinking");
    expect(dock()).toHaveAttribute("data-busy", "1");

    rerender(
      <LanguageProvider>
        <AgentPresenceProvider>
          <AgentPresence />
          <Posing state="builder" />
          <Doing state="thinking" label="reading" active={false} />
        </AgentPresenceProvider>
      </LanguageProvider>
    );
    expect(stateOf()).toContain("builder");
  });

  it("is withdrawn when the screen that declared it leaves", () => {
    const { rerender } = render(
      <LanguageProvider>
        <AgentPresenceProvider>
          <AgentPresence />
          <Posing state="builder" active />
        </AgentPresenceProvider>
      </LanguageProvider>
    );
    expect(stateOf()).toContain("builder");

    rerender(
      <LanguageProvider>
        <AgentPresenceProvider>
          <AgentPresence />
          <Posing state="builder" active={false} />
        </AgentPresenceProvider>
      </LanguageProvider>
    );
    expect(stateOf()).toContain("idle");
    expect(dock()).toHaveAttribute("data-posed", "0");
  });
});

/* ── Which corner it stands in ───────────────────────────────────────────
   A screen with a composer at the foot of it declares the far side, so the
   agent is beside the conversation rather than opposite it. */

function Facing({ side, active = true }) {
  useAgentSide(side, active);
  return null;
}

describe("the side the agent stands on", () => {
  it("is the right-hand corner unless a screen says otherwise", () => {
    shell(<Doing state="thinking" label="x" />);
    expect(dock()).toHaveAttribute("data-side", "right");
  });

  it("moves when a screen with a chat bar asks it to", () => {
    shell(<Facing side="left" />);
    expect(dock()).toHaveAttribute("data-side", "left");
  });

  it("goes back when that screen leaves", () => {
    const { rerender } = render(
      <LanguageProvider>
        <AgentPresenceProvider>
          <AgentPresence />
          <Facing side="left" active />
        </AgentPresenceProvider>
      </LanguageProvider>
    );
    expect(dock()).toHaveAttribute("data-side", "left");

    rerender(
      <LanguageProvider>
        <AgentPresenceProvider>
          <AgentPresence />
          <Facing side="left" active={false} />
        </AgentPresenceProvider>
      </LanguageProvider>
    );
    expect(dock()).toHaveAttribute("data-side", "right");
  });

  it("is still one element that takes no clicks, on either side", () => {
    shell(<Facing side="left" />);
    /* The stylesheet is not loaded in jsdom, so "takes no clicks" is checked
       the way the older test above checks it: there is nothing in here to
       click. Moving sides must not have introduced anything. */
    expect(document.querySelectorAll("[data-agent-presence]")).toHaveLength(1);
    expect(dock().querySelector("button")).toBeNull();
    expect(dock().querySelector("a")).toBeNull();
  });
});
