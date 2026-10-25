/* Loaded before every test file (`vite.config.js` → `test.setupFiles`).
 *
 * Two things, both global on purpose:
 *
 * 1. `@testing-library/jest-dom` — the DOM matchers (`toBeDisabled`,
 *    `toBeInTheDocument`, …). Registered once here rather than imported per
 *    file, which is what keeps a missing import from silently turning an
 *    assertion into `undefined is not a function` in one file only.
 * 2. `cleanup` after each test. Testing Library renders into a container it
 *    appends to `document.body`; without this, the second test in a file
 *    queries a DOM still holding the first test's component and gets
 *    "found multiple elements" for a button that is only rendered once.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
