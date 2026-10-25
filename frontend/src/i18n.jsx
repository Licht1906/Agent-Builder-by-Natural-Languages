import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { VI } from "./i18n-vi";

/* ============================================================
   Two languages, one dictionary, keyed by the English sentence.

   The key **is** the English string. That is unusual and it is deliberate: this
   app already had several hundred sentences written inline in JSX, and inventing
   a short name for each would have meant three places to keep in step — the key,
   the English, and the Vietnamese. Keyed this way there are two, and a sentence
   nobody has translated yet renders in English rather than as `dashboard.title`.

   The cost is real and worth naming: **editing an English string silently drops
   its translation**, because the key moved. `npm run i18n:check` (see
   `scripts/i18n-check.mjs`) lists every `t(...)` with no Vietnamese entry, which
   is how that gets caught rather than noticed.
   ============================================================ */

const STORAGE_KEY = "agentlab.lang";

export const LANGUAGES = [
  { id: "en", label: "EN", name: "English" },
  { id: "vi", label: "VI", name: "Tiếng Việt" },
];

/** The language to open in: what was chosen before, else what the browser asks
 *  for, else English. A Vietnamese-speaking user should not have to find the
 *  toggle before the app is readable. */
function initialLanguage() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "en" || stored === "vi") return stored;
    return (navigator.language || "").toLowerCase().startsWith("vi") ? "vi" : "en";
  } catch {
    return "en";
  }
}

const LanguageContext = createContext({ lang: "en", setLang: () => {}, t: (text) => text });

export function LanguageProvider({ children }) {
  const [lang, setLangState] = useState(initialLanguage);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const setLang = useCallback((next) => {
    setLangState(next);
    try { localStorage.setItem(STORAGE_KEY, next); } catch { /* private mode */ }
  }, []);

  /* `vars` fills `{name}` placeholders. Done here rather than by template
     literals at the call site because a sentence's word order changes between
     languages — "version #3 of Foo" and "phiên bản #3 của Foo" put the pieces in
     different places, and a pre-joined string cannot be reordered by a
     translator. */
  const t = useCallback(
    (text, vars) => {
      const translated = lang === "vi" ? VI[text] || text : text;
      if (!vars) return translated;
      return translated.replace(/\{(\w+)\}/g, (whole, name) =>
        Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole
      );
    },
    [lang]
  );

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

/** The whole API a component needs: `t` to translate, `lang` when a component
 *  has to branch on it (a date format, say), `setLang` for the toggle. */
export function useLang() {
  return useContext(LanguageContext);
}

/** Translate outside a component — for module-level tables of labels.
 *
 *  Takes `t` explicitly rather than reaching for the context, so a caller cannot
 *  accidentally read a stale language: there is no hook here to subscribe with,
 *  and a module-level lookup would be resolved once at import and never again. */
export const translateAll = (t, items, field = "label") =>
  items.map((item) => ({ ...item, [field]: t(item[field]) }));
