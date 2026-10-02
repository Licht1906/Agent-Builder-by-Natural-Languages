import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import { LanguageProvider } from './i18n'

/* The stored theme, applied before the first paint.
 *
 * Before React renders, so a dark-mode user never sees one frame of the light
 * palette on the way in. Falls back to the operating system's preference, which
 * is what somebody who has never touched the toggle actually meant. */
try {
  const stored = localStorage.getItem('agentlab.theme')
  const dark = stored ? stored === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
} catch {
  document.documentElement.dataset.theme = 'light'
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <LanguageProvider>
      <App />
    </LanguageProvider>
  </React.StrictMode>,
)
