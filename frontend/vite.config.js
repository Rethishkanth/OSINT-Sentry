import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Pinned so the console is always at one known address. Without this Vite
    // silently bumps to 5174+ when the port is busy, which makes the URL a
    // guessing game.
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
