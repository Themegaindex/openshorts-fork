import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // Read only backend configuration from the repository .env. This stays in
  // the Vite server process; no token is defined in browser import.meta.env.
  const backendEnv = loadEnv(mode, fileURLToPath(new URL('..', import.meta.url)), 'OPENSHORTS_')
  const backendTarget = process.env.OPENSHORTS_BACKEND_URL || backendEnv.OPENSHORTS_BACKEND_URL || 'http://127.0.0.1:8000'
  const backendToken = process.env.OPENSHORTS_API_TOKEN || backendEnv.OPENSHORTS_API_TOKEN
  const backendHeaders = backendToken ? { 'X-OpenShorts-Token': backendToken } : {}
  return {
  plugins: [react()],
  server: {
    // No extra allowedHosts: Vite then accepts only localhost and IP
    // addresses. The proxy adds the API token to every request, so a foreign
    // host name (the upstream project's domain was listed here) would open a
    // DNS-rebinding path to the authenticated API.
    proxy: {
      '/api': {
        target: backendTarget,
        headers: backendHeaders,
        changeOrigin: true,
      },
      '/videos': {
        target: backendTarget,
        headers: backendHeaders,
        changeOrigin: true,
      },
      '/thumbnails': {
        target: backendTarget,
        headers: backendHeaders,
        changeOrigin: true,
      },
      '/gallery': {
        target: backendTarget,
        headers: backendHeaders,
        changeOrigin: true,
      },
      '/video': {
        target: backendTarget,
        headers: backendHeaders,
        changeOrigin: true,
      }
    }
  }
  }
})
