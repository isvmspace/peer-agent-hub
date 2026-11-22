import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'node:path'

export default defineConfig({
  plugins: [vue()],
  base: '/console/',
  server: {
    proxy: { '/api': 'http://127.0.0.1:8790' },
  },
  build: {
    outDir: resolve(__dirname, '../hub/src/agent_hub/web'),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // the UI library and markdown renderer change rarely: separate files the browser can keep cached
        manualChunks: id => {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('naive-ui') || id.includes('vueuc') || id.includes('css-render') || id.includes('date-fns')) return 'vendor-ui'
          if (id.includes('markdown-it') || id.includes('linkify') || id.includes('entities') || id.includes('mdurl') || id.includes('punycode')) return 'vendor-markdown'
          if (id.includes('@lucide')) return 'vendor-icons'
          return 'vendor'
        },
      },
    },
  },
})
