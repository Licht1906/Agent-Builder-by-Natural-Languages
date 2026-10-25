import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentMascot, AgentBusy } from "./mascot";
import { LanguageProvider } from "../i18n";

/* The mascot is CSS-animated pixels; what a test can hold still is the contract
 * between a screen and this component: the state names, the silence toward
 * assistive tech, and the gaze — the one piece with actual logic in it. */

const mascotOf = (container) => container.querySelector(".al-mascot");

describe("states", () => {
  it("wears the class of the state it was asked for", () => {
    const { container } = render(<AgentMascot state="processing" />);
    expect(mascotOf(container).className).toContain("al-mascot--processing");
  });

  it("falls back to idle for a state the design package never drew", () => {
    /* A typo'd state must not silently render a mascot with no animation class
       at all — idle is the drawn default. */
    const { container } = render(<AgentMascot state="jubilant" />);
    expect(mascotOf(container).className).toContain("al-mascot--idle");
  });

  it("shows both layers from the design package", () => {
    const { container } = render(<AgentMascot state="builder" />);
    const sources = [...container.querySelectorAll("img")].map((img) => img.getAttribute("src"));
    expect(sources).toContain("/agentlab-mascot/agentlab-mascot.png");
    expect(sources).toContain("/agentlab-mascot/agentlab-blueprint.png");
  });
});

describe("assistive text", () => {
  it("is silent by default — the words are already on the screen", () => {
    const { container } = render(<AgentMascot state="welcome" />);
    expect(mascotOf(container)).toHaveAttribute("aria-hidden", "true");
    expect(mascotOf(container)).not.toHaveAttribute("role");
  });

  it("announces itself only when handed a label", () => {
    render(<AgentMascot state="welcome" label="Agentlab mascot welcomes you" />);
    expect(screen.getByRole("img", { name: "Agentlab mascot welcomes you" })).toBeInTheDocument();
  });
});

describe("gaze", () => {
  it("leans the robot toward the pointer when follow is on", async () => {
    const { container } = render(<AgentMascot state="welcome" follow />);
    const gaze = container.querySelector(".al-mascot__gaze");
    fireEvent.mouseMove(window, { clientX: 900, clientY: 500 });
    await waitFor(() => expect(gaze.style.transform).toMatch(/translate\(.+rotate\(/s));
  });

  it("stays put without follow", async () => {
    const { container } = render(<AgentMascot state="welcome" />);
    const gaze = container.querySelector(".al-mascot__gaze");
    fireEvent.mouseMove(window, { clientX: 900, clientY: 500 });
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    expect(gaze.style.transform).toBe("");
  });
});

describe("AgentBusy", () => {
  it("keeps the word in the DOM beside the animation", () => {
    /* `Chat.test.jsx` greps a thinking bubble for the word — the animation must
       never become the only signal. */
    render(<LanguageProvider><AgentBusy label="thinking" /></LanguageProvider>);
    expect(screen.getByText(/thinking/)).toBeInTheDocument();
  });

  it("wears whichever state the operation is in", () => {
    const { container } = render(<LanguageProvider><AgentBusy state="builder" label="composing" /></LanguageProvider>);
    expect(container.querySelector(".al-mascot").className).toContain("al-mascot--builder");
  });
});
