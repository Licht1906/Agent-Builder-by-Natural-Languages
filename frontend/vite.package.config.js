import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The exported package's chat (DS-17) — a second, separate build of the same
// source tree. Its own root so the output is `index.html` + `assets/` and
// nothing of the platform's `public/`; the runtime serves `dist-package/` at `/`
// and its API at the same origin (`/ui-config`, `/chat`, `/approvals`), so no
// URL is compiled in and no CORS is involved.
export default defineConfig({
  plugins: [react()],
  root: 'package',
  publicDir: false,
  base: '/',
  build: {
    outDir: '../dist-package',
    emptyOutDir: true,
  },
  server: {
    port: 5174,
    fs: { allow: ['..'] },
    proxy: Object.fromEntries(
      ['/ui-config', '/chat', '/approvals', '/health'].map((path) => [path, { target: process.env.PACKAGE_URL || 'http://localhost:8080', changeOrigin: true }]),
    ),
  },
})
