import React from "react";
import ReactDOM from "react-dom/client";
import PackageChat from "./PackageChat.jsx";
import { LanguageProvider } from "../i18n";

/* The exported package's page (DS-17), built on its own by `npm run build:package`
 * and served by the package's runtime at `/`. Same theme rule as the platform's
 * `main.jsx`: the stored or the system's palette, before the first paint. */
try {
  const stored = localStorage.getItem("agentlab.theme");
  const dark = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
} catch {
  document.documentElement.dataset.theme = "light";
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <LanguageProvider>
      <PackageChat />
    </LanguageProvider>
  </React.StrictMode>,
);
