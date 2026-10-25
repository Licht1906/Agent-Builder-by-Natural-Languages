import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { BRAND, LogoMark, BrandLockup } from "./brand";

/* The brand is pixels on disk plus a table of names pointing at them, and the
   two can fall out of step silently: a renamed PNG still renders, just as a
   broken image nobody notices until it ships. **Nothing currently checks that
   the files behind these names exist** — the script that cut them was not kept
   with the repo, so neither was its test. These are about the promises this
   module makes to the screens importing it; the pixels are on trust. */

describe("assistive text", () => {
  /* The mascot and the mark illustrate wording that is already on the screen.
     Announcing them as well makes a screen reader read the same message twice,
     the second time as "image". Opting back in is explicit. */
  it("hides the mark beside a visible wordmark, and labels it when it stands alone", () => {
    const { container, rerender } = render(<BrandLockup />);
    expect(screen.getByText("Agentlab")).toBeInTheDocument();
    expect(container.querySelector("img")).toHaveAttribute("aria-hidden", "true");

    rerender(<BrandLockup showName={false} />);
    expect(screen.getByAltText("Agentlab")).toBeInTheDocument();
  });
});

describe("logo mark sizes", () => {
  const src = (size) => render(<LogoMark size={size} />).container.querySelector("img").getAttribute("src");

  it("hands small slots the small-size cut and larger ones the full mark", () => {
    /* The switch the brand makes, not the caller: below ~40px the full mark's
       blueprint sheet, page curl and mascot merge into one blue rectangle, so a
       sidebar tile gets the head-only cut and a hero gets the whole drawing. */
    expect(src(20)).toMatch(/icon-small/);
    expect(src(28)).toMatch(/icon-small/);
    expect(src(48)).toMatch(/logo-mark/);
    expect(src(120)).toMatch(/logo-mark/);
  });

  it("serves the smallest file that still has pixels to spare at 2x", () => {
    /* A 28px tile asking for the 1266px master is a megabyte of PNG the browser
       then throws away. Each size gets the next file up from twice its own. */
    expect(src(28)).toBe("/brand/icon-small-64.png");
    expect(src(48)).toBe("/brand/logo-mark-128.png");
  });

  it("never asks for a file that was not cut", () => {
    /* Beyond the largest cut size the request has to fall back, not 404. */
    expect(src(900)).toBe("/brand/logo-mark-512.png");
  });
});

describe("the files behind the names", () => {
  /* The one failure this module cannot express in JSX: a mark size naming a PNG
     that `public/brand/` does not hold. jsdom will happily render the broken
     <img>, and so will the browser — as a blank gap nobody spots in review. */
  const served = (src) => resolve(process.cwd(), `public${src}`);
  const srcOf = (element) => {
    const { container, unmount } = render(element);
    const src = container.querySelector("img").getAttribute("src");
    unmount();
    return src;
  };

  it("has a cut PNG for every mark size, plus the icons the browser asks for by name", () => {
    const sizes = [16, 28, 32, 64, 128, 256, 512, 900];
    const missing = sizes.filter((size) => !existsSync(served(srcOf(<LogoMark size={size} />))));
    expect(missing).toEqual([]);
    for (const name of ["/brand/favicon.ico", "/brand/apple-touch-icon.png"]) {
      expect(existsSync(served(name)), name).toBe(true);
    }
  });
});

describe("brand palette", () => {
  it("holds the identity board's four colours", () => {
    expect(BRAND).toEqual({ ink: "#05264f", blue: "#1d72fc", cyan: "#5ee8fd", paper: "#f8fbfe" });
  });
});
