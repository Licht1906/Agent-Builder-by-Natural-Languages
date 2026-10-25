/* Which `t(...)` keys have no Vietnamese entry.
 *
 * The dictionary is keyed by the English sentence (see `src/i18n.jsx`), which
 * buys one file instead of three and costs one thing: **editing an English
 * string silently drops its translation**, because the key moved and the new one
 * falls back to English. Nothing on screen breaks, which is exactly why it needs
 * a checker — the failure is invisible to anyone reading in English.
 *
 * Reports rather than fails by default. A sentence added today and translated
 * tomorrow is a normal state for this file to be in; pass `--strict` (CI) to
 * exit non-zero instead.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, extname, basename } from "node:path";
import { pathToFileURL } from "node:url";

const SRC = new URL("../src/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const KEY = /\bt\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")\s*[,)]/g;

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return walk(full);
    return [".js", ".jsx"].includes(extname(name)) && !basename(name).startsWith("i18n") ? [full] : [];
  });
}

const used = new Map(); // key -> first file that used it
for (const file of walk(SRC)) {
  const text = readFileSync(file, "utf8");
  for (const match of text.matchAll(KEY)) {
    const raw = (match[1] ?? match[2]).replace(/\\'/g, "'").replace(/\\"/g, '"');
    if (!used.has(raw)) used.set(raw, file.slice(SRC.length));
  }
}

const { VI } = await import(pathToFileURL(join(SRC, "i18n-vi.js")).href);

const missing = [...used].filter(([key]) => !(key in VI));
const unused = Object.keys(VI).filter((key) => !used.has(key));

console.log(`t() keys in use : ${used.size}`);
console.log(`translated      : ${used.size - missing.length}`);
console.log(`missing         : ${missing.length}`);
console.log(`unused entries  : ${unused.length}  (data-table labels are expected here)`);

if (missing.length) {
  console.log("\nNo Vietnamese entry:");
  for (const [key, file] of missing) console.log(`  ${file}  ${JSON.stringify(key)}`);
}

if (process.argv.includes("--strict") && missing.length) process.exit(1);
