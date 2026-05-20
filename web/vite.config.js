import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    port: 5173,
    host: true,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // Keep the output simple — single JS bundle is fine for this size
    // and avoids Cloudflare Pages serving issues with code-split chunks
    // that occasionally get gzipped twice.
    rollupOptions: {
      output: {
        manualChunks: undefined,
      },
    },
  },
})
