import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";

// Tauri serves the dev build from a fixed port and needs a real host.
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  clearScreen: false,
  server: { port: 5183, strictPort: true },
  build: { target: "es2021", sourcemap: false },
});
