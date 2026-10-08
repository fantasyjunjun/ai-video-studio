import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发：Vite(5173) 把 /api 代理到 FastAPI(8000)
// 生产：vite build 出 dist/，由 FastAPI 单端口托管，前端脚本里统一用相对路径 /api
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 2000,
  },
});
