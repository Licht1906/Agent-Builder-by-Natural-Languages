/* Build `smoke-entry.jsx` for the server and run it.
 *
 * Vite does the transforming so the entry can be ordinary JSX importing
 * ordinary source — no second build config to keep in step with the real one.
 *
 * The browser globals below are the minimum the render path touches
 * (`document.documentElement.dataset` for the stored theme, `window.location`
 * for the shareable agent link, `localStorage`/`navigator` for the stored
 * language). Stubbed rather than jsdom: the point is to execute component
 * bodies, and anything needing a real DOM is in an effect, which
 * `renderToString` never runs.
 */

import { build } from "vite";
import react from "@vitejs/plugin-react";
import { rmSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

const OUT = new URL("../.smoke/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

const store = new Map();
globalThis.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
  removeItem: (key) => store.delete(key),
};
// `navigator` is a getter-only global on modern Node, so it is defined rather
// than assigned. `configurable` so a second run in the same process can replace it.
Object.defineProperty(globalThis, "navigator", {
  value: { language: "en-US", clipboard: { writeText: async () => {} } },
  configurable: true,
  writable: true,
});
globalThis.document = {
  documentElement: { dataset: {}, lang: "en" },
  createElement: () => ({ style: {}, click() {}, remove() {}, setAttribute() {} }),
  body: { appendChild() {}, removeChild() {} },
  addEventListener() {},
  removeEventListener() {},
};
globalThis.window = {
  location: { origin: "http://localhost:5173", pathname: "/", search: "" },
  matchMedia: () => ({ matches: false }),
  addEventListener() {},
  removeEventListener() {},
};

rmSync(OUT, { recursive: true, force: true });
await build({
  logLevel: "error",
  plugins: [react()],
  build: {
    ssr: true,
    outDir: ".smoke",
    emptyOutDir: true,
    rollupOptions: { input: new URL("./smoke-entry.jsx", import.meta.url).pathname, output: { format: "es" } },
  },
});

const { run } = await import(pathToFileURL(join(OUT, "smoke-entry.js")).href);
const { count, failures } = run();

console.log(`screens rendered : ${count}`);
console.log(`failures         : ${failures.length}`);
for (const failure of failures) console.log("  FAIL:", failure);
rmSync(OUT, { recursive: true, force: true });
process.exit(failures.length ? 1 : 0);
