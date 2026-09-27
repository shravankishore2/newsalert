import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development, `npm run dev` proxies API calls (including the SSE stream) to the
// FastAPI server started with `python -m newsalert demo` or `serve`.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } },
  },
})
