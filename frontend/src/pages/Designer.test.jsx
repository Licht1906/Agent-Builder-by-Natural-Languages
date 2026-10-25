import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "../i18n";
import DesignerPage from "./Designer";
import * as api from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual("../lib/api");
  return {
    ...actual,
    designer: {
      start: vi.fn(),
      answer: vi.fn(),
      buildSpec: vi.fn(),
      // The interview stage fetches pickable answers for each open question;
      // default to "nothing to offer" so tests that don't care get the plain
      // input they always had.
      suggestions: vi.fn().mockResolvedValue({ field: null, multi: false, suggestions: [] }),
    },
  };
});

const draw = () => render(
  <LanguageProvider><DesignerPage onSpecReady={() => {}} /></LanguageProvider>
);

/** An interview the way `designer.start` answers: the description became the
 *  opening answer and the first clarifying question is already open. */
const started = {
  id: 7,
  description: "Read unanswered Gmail and draft replies",
  answered: 1,
  total: 7,
  turns: [],
  open_question: { text: "Who should this agent serve?" },
  conflicts: [],
  complete: false,
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("the home stage", () => {
  it("opens as a doorway: mascot, greeting, one input, example tasks", () => {
    const { container } = draw();

    expect(screen.getByRole("heading", { name: /What are we building today|Hôm nay/i })).toBeInTheDocument();
    expect(container.querySelector(".al-mascot--welcome")).not.toBeNull();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    // No interview yet — so no progress header and no answer box.
    expect(screen.queryByText(/of 7 fields|\/ 7|trên 7/)).toBeNull();
  });

  it("fills the input with the whole example, though the chip only names it", async () => {
    /* The chip carries a short name so three of them fit on one row; what
       lands in the box is the long sentence, because the long sentence is what
       teaches the shape of a good description. */
    const actor = userEvent.setup();
    draw();

    const chip = screen.getByRole("button", { name: /Draft Gmail replies|Soạn nháp trả lời Gmail/i });
    expect(chip).toHaveTextContent(/^(Draft Gmail replies|Soạn nháp trả lời Gmail)$/);

    await actor.click(chip);

    expect(screen.getByRole("textbox").value).toMatch(/unanswered Gmail|Gmail chưa trả lời/i);
    expect(api.designer.start).not.toHaveBeenCalled();
  });

  it("opens with the heading, the box and one row of examples — and no instructions", () => {
    /* The paragraph that used to explain what the designer does was three
       lines above the box on the screen people see most often. */
    draw();
    expect(screen.queryByText(/The designer interviews you|Designer sẽ phỏng vấn bạn/i)).toBeNull();
    expect(screen.queryByText(/Shift+Enter/i)).toBeNull();
  });
});

describe("starting the interview", () => {
  it("sends the description and continues on the same screen as a conversation", async () => {
    const actor = userEvent.setup();
    api.designer.start.mockResolvedValue(started);
    draw();

    await actor.type(screen.getByRole("textbox"), "Read unanswered Gmail and draft replies{Enter}");

    await waitFor(() => expect(api.designer.start).toHaveBeenCalledWith("Read unanswered Gmail and draft replies"));
    // The description is now the first bubble, the open question follows,
    // and the interview header counts the fields.
    expect(await screen.findByText("Read unanswered Gmail and draft replies")).toBeInTheDocument();
    expect(screen.getByText("Who should this agent serve?")).toBeInTheDocument();
    expect(screen.getByText(/1 .*7/)).toBeInTheDocument();
  });

  it("thinks out loud while the first description is being read", async () => {
    const actor = userEvent.setup();
    let resolveStart;
    api.designer.start.mockReturnValue(new Promise((resolve) => { resolveStart = resolve; }));
    const { container } = draw();

    await actor.type(screen.getByRole("textbox"), "Summarize Drive files{Enter}");

    // Still on the home stage, but the mascot has switched to thinking.
    expect(container.querySelector(".al-mascot--thinking")).not.toBeNull();
    expect(screen.getByText(/reading your description|đang đọc/i)).toBeInTheDocument();

    resolveStart(started);
    await screen.findByText("Who should this agent serve?");
  });

  it("keeps the words when the start fails, so nothing typed is lost", async () => {
    const actor = userEvent.setup();
    api.designer.start.mockRejectedValue(new Error("designer is down"));
    draw();

    await actor.type(screen.getByRole("textbox"), "Triage tickets{Enter}");

    expect(await screen.findByText(/designer is down/)).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("Triage tickets");
  });
});

describe("pickable answers for the open question", () => {
  it("offers the suggestions, flags the first recommended, and sends the picks joined", async () => {
    const actor = userEvent.setup();
    api.designer.start.mockResolvedValue(started);
    api.designer.suggestions.mockResolvedValue({
      field: "users",
      multi: true,
      suggestions: [
        { text: "Support agents", recommended: true },
        { text: "Sales reps", recommended: false },
      ],
    });
    api.designer.answer.mockResolvedValue({ ...started, open_question: null, answered: 2 });
    draw();

    await actor.type(screen.getByRole("textbox"), "Read unanswered Gmail and draft replies{Enter}");

    const recommended = await screen.findByRole("button", { name: /Support agents/ });
    expect(recommended).toHaveTextContent(/Recommended|Khuyến nghị/);

    await actor.click(recommended);
    await actor.click(screen.getByRole("button", { name: /Sales reps/ }));
    await actor.click(screen.getByRole("button", { name: /Send|Gửi/ }));

    await waitFor(() =>
      expect(api.designer.answer).toHaveBeenCalledWith(7, "Support agents\nSales reps"),
    );
  });

  it("stays the plain input when nothing is suggested", async () => {
    const actor = userEvent.setup();
    api.designer.start.mockResolvedValue(started);
    api.designer.suggestions.mockResolvedValue({ field: "users", multi: true, suggestions: [] });
    draw();

    await actor.type(screen.getByRole("textbox"), "Read unanswered Gmail and draft replies{Enter}");

    await screen.findByText("Who should this agent serve?");
    expect(screen.queryByText(/Pick any that fit|Chọn các ý/)).toBeNull();
  });
});
