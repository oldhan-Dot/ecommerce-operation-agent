// Vite 开发配置：本机固定端口与 API 代理。

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // 仅监听本机；端口占用时直接报错，不自动换端口造成代理错配。
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // 浏览器统一访问前端同源 /api，由开发服务器转发给 FastAPI。
    // 隔离验收可以指定测试 API；默认仍连接本机开发端口。
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})
