import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The browser never talks to FastAPI directly: everything under `/api` is
// proxied through this dev server. That is what keeps CORS out of the picture
// entirely — the page and the API share an origin — and it means the production
// build works behind any reverse proxy that maps `/api` to the backend, with no
// URL compiled into the bundle.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react()],
    // Component tests run in jsdom, not a browser: what is worth asserting
    // here is which API helper a button calls and what the screen does with
    // the answer, and none of that needs a real rendering engine. A browser
    // suite (Playwright) would test a different thing — the app, the backend
    // and the database together — and is not what this config is for.
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.js',
      include: ['src/**/*.test.{js,jsx}'],
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: env.BACKEND_URL || 'http://localhost:8000',
          changeOrigin: true,
          // Sandbox runs and chat are Server-Sent Events. Buffering them would
          // turn a live trace into one lump arriving when the run is over.
          configure: (proxy) => {
            proxy.on('proxyRes', (proxyRes) => {
              if (String(proxyRes.headers['content-type']).includes('text/event-stream')) {
                proxyRes.headers['cache-control'] = 'no-cache, no-transform'
              }
            })
          },
        },
      },
    },
  }
})
