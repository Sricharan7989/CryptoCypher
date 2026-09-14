import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend runs on :8000. Proxying /api through Vite keeps the frontend
// free of hardcoded hosts and sidesteps CORS entirely during development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
