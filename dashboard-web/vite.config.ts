import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  base: '/memory-viz/',
  // 2026-11-07：emptyOutDir 从 true 改成 false。
  // 原因：webapp/ 里不只有 React 构建产物 —— 还有 3D 小家的 room.html / room-env.js /
  // room-scene.js，以及 656MB 的 models/ 和 anims/。原来的 true 会在每次 build 时
  // 把它们全删掉（今晚就删过一次 room-env.js，room.html 也被还原成旧版）。
  // 代价是旧 assets 会堆积，手动清 assets/ 即可（那个目录只有构建产物）。
  build: { outDir: '../dashboard/webapp', emptyOutDir: false },
  plugins: [react()],
})
