import { useState } from "react";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, afterEach } from "vitest";
import { LanguageProvider } from "../i18n";
import { PageChromeProvider, PageHead, PageShell, Section, Note, SettingsMenu } from "./ui";

afterEach(cleanup);

/* ============================================================
   The frame (ADR-0057).

   These are about the promises the frame makes to the thirty-seven screens
   built on it — not about how any one of them looks. The load-bearing one is
   §4: a page's name is printed once, and the page is not the thing that
   prints it.
   ============================================================ */

/** A stand-in for AppLayout: the header, the action slot, and the page. */
function Shell({ fallbackTitle = "Fallback", children }) {
  const [head, setHead] = useState(null);
  const [slot, setSlot] = useState(null);
  return (
    <div>
      <header>
        <h1>{head?.title || fallbackTitle}</h1>
        {head?.hint ? <span data-testid="head-hint">{head.hint}</span> : null}
        <div data-testid="slot" ref={setSlot} />
      </header>
      <PageChromeProvider setHead={setHead} actionSlot={slot}>
        {children}
      </PageChromeProvider>
    </div>
  );
}

describe("PageHead — the page's name, printed once", () => {
  it("hands its title to the shell header and leaves nothing in the page body", () => {
    render(
      <Shell>
        <PageShell>
          <PageHead title="Agent của bạn" />
          <p>rows</p>
        </PageShell>
      </Shell>
    );
    /* One heading on the screen, and it is the header's. A second <h1> here is
       exactly the duplication the whole ADR exists to remove, so the count is
       the assertion. */
    const headings = screen.getAllByRole("heading", { name: "Agent của bạn" });
    expect(headings).toHaveLength(1);
    expect(headings[0].closest("header")).not.toBeNull();
  });

  it("overrides the shell's own default title, and gives it back on unmount", () => {
    const { rerender } = render(
      <Shell fallbackTitle="Knowledge Bases">
        <PageShell><PageHead title="Kệ tài liệu nhân sự" /></PageShell>
      </Shell>
    );
    expect(screen.getByRole("heading")).toHaveTextContent("Kệ tài liệu nhân sự");

    /* Leaving a detail screen must not leave its subject's name in the header
       of whatever is on screen next. */
    rerender(<Shell fallbackTitle="Knowledge Bases"><PageShell /></Shell>);
    expect(screen.getByRole("heading")).toHaveTextContent("Knowledge Bases");
  });

  it("puts the page's actions in the header beside the name, not in the body", async () => {
    render(
      <Shell>
        <PageShell>
          <PageHead title="Agent của bạn">
            <button type="button">Tạo agent</button>
          </PageHead>
          <p>rows</p>
        </PageShell>
      </Shell>
    );
    const button = screen.getByRole("button", { name: "Tạo agent" });
    expect(screen.getByTestId("slot")).toContainElement(button);
    expect(button.closest("main")).toBeNull();
  });

  it("carries the hint rather than printing the explanation on the page", () => {
    render(
      <Shell>
        <PageShell><PageHead title="Kết nối" hint="Credential vào Vault." /></PageShell>
      </Shell>
    );
    /* ADR-0056 §8 — every explanation retreats behind a mark. */
    expect(screen.getByTestId("head-hint")).toHaveTextContent("Credential vào Vault.");
  });

  it("falls back to a heading in the body when there is no shell to hand it to", () => {
    /* Landing, sign-in and register render outside AppLayout: no header
       exists, so the title has nowhere to go but the page. */
    render(<PageHead title="Đăng nhập"><button type="button">Gửi</button></PageHead>);
    expect(screen.getByRole("heading", { name: "Đăng nhập" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Gửi" })).toBeInTheDocument();
  });
});

describe("PageShell — the page's margins", () => {
  it("names its reading width instead of stating a number", () => {
    const { container, rerender } = render(<PageShell>x</PageShell>);
    const inner = () => container.querySelector(".ab-page-inner");
    expect(inner().className).not.toMatch(/narrow|wide/);

    rerender(<PageShell width="narrow">x</PageShell>);
    expect(inner()).toHaveClass("narrow");

    rerender(<PageShell width="wide">x</PageShell>);
    expect(inner()).toHaveClass("wide");
  });

  it("owns the scroll, so no page has to work out where its own scroll lives", () => {
    const { container } = render(<PageShell>x</PageShell>);
    expect(container.querySelector(".ab-page")).toHaveClass("ab-scrollbar");
  });
});

describe("Section — the only thing that spaces two groups", () => {
  it("labels a group and keeps its controls with the label", () => {
    render(
      <Section title="Agent bạn được dùng" actions={<button type="button">Tải lại</button>}>
        <p>rows</p>
      </Section>
    );
    const head = screen.getByRole("heading", { name: /Agent bạn được dùng/ }).parentElement;
    expect(head).toHaveClass("ab-section-head");
    expect(head).toContainElement(screen.getByRole("button", { name: "Tải lại" }));
  });

  it("draws no head at all when it has neither a label nor controls", () => {
    const { container } = render(<Section><p>rows</p></Section>);
    expect(container.querySelector(".ab-section-head")).toBeNull();
  });
});

describe("Note — a refusal that says it can be opened", () => {
  const REFUSAL = "Điểm 0.62 dưới ngưỡng 0.75 — còn 3 case nữa phải qua. Chạy lại sau khi sửa.";

  it("says nothing when there is nothing to say", () => {
    const { container } = render(<Note tone="warn" fold text={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("closes to its mark alone — no sentence on screen, every word still in the DOM", () => {
    /* ADR-0011 §5 buys the score, the bar, the distance in whole cases and the
       next move. Folding may hide them; it may never drop them — so the text
       stays in the markup, and `title` / `aria-label` carry it for anything
       that never clicks. What the eye gets is a mark. */
    render(<Note tone="warn" fold text={REFUSAL} />);
    const note = screen.getByRole("button");
    expect(note).toHaveClass("folded");
    expect(note).toHaveTextContent(REFUSAL);
    expect(note).toHaveAttribute("title", REFUSAL);
    expect(note).toHaveAttribute("aria-label", REFUSAL);
  });

  it("still announces itself as a control once the words are gone", () => {
    /* A glyph with no accessible name is a button nobody can find. */
    render(<Note tone="warn" fold text={REFUSAL} label="Cổng duyệt từ chối" />);
    expect(screen.getByRole("button", { name: "Cổng duyệt từ chối" })).toBeInTheDocument();
  });

  it("is a button with an expanded state, which is what says it can be opened", async () => {
    const actor = userEvent.setup();
    render(<Note tone="warn" fold text={REFUSAL} />);
    const note = screen.getByRole("button");
    expect(note).toHaveAttribute("aria-expanded", "false");

    await actor.click(note);
    expect(note).toHaveAttribute("aria-expanded", "true");
    expect(note).not.toHaveClass("folded");
  });

  it("folds itself again when the refusal changes", async () => {
    /* Otherwise "open" is sticky for the session and the fold stops being a
       fold — which is how a note that was already clamped got read as one that
       always shows in full. */
    const actor = userEvent.setup();
    const { rerender } = render(<Note tone="warn" fold text={REFUSAL} />);
    await actor.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");

    rerender(<Note tone="warn" fold text="Một lời từ chối khác." />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("is not a button when it is not foldable — nothing to press, so nothing that looks pressable", () => {
    render(<Note tone="warn">Ghi chú</Note>);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Ghi chú")).toBeInTheDocument();
  });
});

/* ── Settings: one mark, one menu ────────────────────────────────────────
   Theme and language used to be two permanent header controls. Behind a menu
   they are cheaper, but only if both ways out of the menu work — a popover you
   can open and not dismiss is the one failure mode of the pattern. */

const withLang = (ui) => render(<LanguageProvider>{ui}</LanguageProvider>);

describe("the settings menu", () => {
  it("is one control until it is asked for", () => {
    withLang(<SettingsMenu />);
    const gear = screen.getByRole("button", { name: /Settings|Cài đặt/i });
    expect(gear).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("keeps both settings one click in", async () => {
    const actor = userEvent.setup();
    withLang(<SettingsMenu />);
    await actor.click(screen.getByRole("button", { name: /Settings|Cài đặt/i }));

    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Theme|Giao diện/i })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Language|Ngôn ngữ/i })).toBeInTheDocument();
  });

  it("actually sets the theme, and remembers it", async () => {
    const actor = userEvent.setup();
    withLang(<SettingsMenu />);
    await actor.click(screen.getByRole("button", { name: /Settings|Cài đặt/i }));
    await actor.click(screen.getByRole("button", { name: /Dark|Tối/i }));

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("agentlab.theme")).toBe("dark");

    await actor.click(screen.getByRole("button", { name: /Light|Sáng/i }));
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("closes on Escape", async () => {
    const actor = userEvent.setup();
    withLang(<SettingsMenu />);
    await actor.click(screen.getByRole("button", { name: /Settings|Cài đặt/i }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on a pointer landing outside it", async () => {
    const actor = userEvent.setup();
    withLang(<div><SettingsMenu /><button type="button">elsewhere</button></div>);
    await actor.click(screen.getByRole("button", { name: /Settings|Cài đặt/i }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await actor.click(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
