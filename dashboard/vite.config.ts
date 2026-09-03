import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the dashboard runs on :5173 and the backend on :8000, so both the REST
// API and the WebSocket are proxied through the dev server. That keeps the frontend code
// origin-relative ("/ws", "/api/...") and identical to production, where the backend
// serves the built dashboard from its own origin and no proxy exists at all.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
