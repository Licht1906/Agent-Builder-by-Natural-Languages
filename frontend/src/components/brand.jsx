/* Agentlab's mark — the logo, its small-size cut, and the lockup.

   The animated mascot lives in `mascot.jsx` (the JSX port of the design/
   package) and is what every screen now uses; this module keeps the assets the
   package does not carry: the favicon-grade marks under `public/brand/` and the
   mark+wordmark lockup the shells share. Nobody outside this file types those
   filenames.

   The PNGs under `public/brand/` were cut from the two 1254px sheets in
   `design/assets/`, and **the script that cut them was not kept** — they are
   correct and checked in, but re-cutting them means writing that step again. */

/** The identity board's four colours, exact.
 *
 *  Also mirrored into CSS custom properties in `theme.js` (`--brand-*`) for
 *  stylesheet use; this copy is for the places that need the literal value in
 *  JS — a canvas fill, an inline SVG, an image placeholder. */
export const BRAND = {
  ink: "#05264f",
  blue: "#1d72fc",
  cyan: "#5ee8fd",
  paper: "#f8fbfe",
};

/* Two marks, and the size decides which.
 *
 * The full mark is the mascot standing on its blueprint sheet — a drawing with
 * a robot, a page curl, a grid and two construction letters in it. Below about
 * 40px those stop being separate things: at 16px it is a blue rectangle. So the
 * brand also draws the mascot's head on its own, outline thickened, nothing
 * else in frame, and that is what small slots get. Callers ask for a size and
 * are handed whichever one survives it, because "which cut of the mark is this"
 * is a question about the brand, not about the sidebar.
 *
 * Within a cut, each file exists at several resolutions so a 28px tile is not a
 * 1266px PNG scaled down in the browser; the smallest file with pixels to spare
 * at 2× device pixel ratio wins. */
const SMALL_MARK_ABOVE = 40;
const MARK_SIZES = { "logo-mark": [32, 64, 128, 256, 512], "icon-small": [32, 64, 256] };

function markSrc(size, density = 1) {
  /* The cut follows the size the mark is *drawn at*, never the density it is
     drawn with: a 28px tile on a retina screen is still a 28px tile, and asking
     for twice the pixels must not quietly hand back the other artwork. */
  const cut = size < SMALL_MARK_ABOVE ? "icon-small" : "logo-mark";
  const steps = MARK_SIZES[cut];
  const step = steps.find((s) => s >= size * density) || steps[steps.length - 1];
  return `/brand/${cut}-${step}.png`;
}

/** The logo mark: the mascot on its blueprint sheet, square, transparent.
 *
 *  Decorative wherever a readable "Agentlab" sits beside it, which is every
 *  place it is used today — so it is hidden from assistive tech by default and
 *  the word carries the name. Pass `title` only when the mark stands alone. */
export function LogoMark({ size = 28, title, style }) {
  return (
    <img
      /* Density 2, not 1. `srcSet` is a hint a browser may decline — and
         anything that never reads it at all (an email client, a screenshot, a
         crawler) falls back to `src`. Handing that fallback the 1× cut makes the
         mark soft on every retina screen, which is the one place it is most
         looked at. The rule this module states — *the smallest file with pixels
         to spare at 2×* — is a rule about the fallback too. */
      src={markSrc(size, 2)}
      srcSet={`${markSrc(size, 1)} 1x, ${markSrc(size, 2)} 2x`}
      width={size}
      height={size}
      alt={title || ""}
      aria-hidden={title ? undefined : true}
      draggable={false}
      style={{ width: size, height: size, flex: "none", display: "block", ...style }}
    />
  );
}

/** Mark + wordmark, the lockup every shell puts in its top-left corner.
 *
 *  One component rather than the four copies of "image, gap, 'Agentlab'" the
 *  sidebar, the landing header and both auth panels used to hold — they drifted
 *  by a pixel and a font-size each, which is exactly the drift a brand is for
 *  preventing. `tone="light"` is the version that sits on the dark panel. */
export function BrandLockup({ size = 28, tone = "dark", showName = true, style }) {
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, ...style }}>
      <LogoMark size={size} title={showName ? undefined : "Agentlab"} />
      {showName && (
        <span
          style={{
            fontFamily: "var(--font-heading)",
            fontSize: Math.round(size * 0.68),
            lineHeight: 1.1,
            whiteSpace: "nowrap",
            color: tone === "light" ? "#f2f2f3" : "var(--color-text)",
          }}
        >
          Agentlab
        </span>
      )}
    </span>
  );
}
