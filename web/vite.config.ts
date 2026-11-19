import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => ({
  ...(mode === 'demo' && { base: './', build: { outDir: 'dist-demo' } }),
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': process.env.REHEARSAL_API_URL || 'http://127.0.0.1:8000',
    },
  },
}))
