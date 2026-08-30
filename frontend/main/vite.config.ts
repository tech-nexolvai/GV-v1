import { defineConfig } from 'vite'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'

// https://vite.dev/config/
// One target for both proxied prefixes, so they cannot drift apart and send half the traffic to a
// server that is not running.
const api = {
  target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000',
  changeOrigin: true,
}

export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] })
  ],
  server: {
    // The client calls `/api/v1/...` relative, so in development that has to reach the API rather
    // than the dev server. A proxy instead of an absolute base URL on purpose: same-origin means no
    // CORS to configure here and none to forget in production, and the browser sends the same
    // request shape in both places.
    proxy: {
      '/api': api,

      // The development upload shim, which is not under `/api` and must not be. It exists only in
      // `scripts/dev_server.py`, because a route accepting file bytes is exactly what the shipped
      // API is tested never to have — see `tests/test_dev_server.py`. Listed separately so that the
      // day it disappears (the S3 backend, #221, gives the browser a real presigned URL and this
      // stops being needed) one line comes out and `/api` is untouched.
      '/_dev': api,
    },
  },
})
