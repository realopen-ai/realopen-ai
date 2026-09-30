import { defineConfig } from "vite";
import { fileURLToPath } from "url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Backend origin for the dev proxy. Defaults to the Docker service hostname;
// set VITE_PROXY_TARGET (e.g. "http://127.0.0.1:8000") when running the
// backend outside Docker.
const backendOrigin = process.env.VITE_PROXY_TARGET || "http://backend:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    watch: {
      usePolling: true,
    },
    proxy: {
      "/api": {
        target: backendOrigin,
        // Sandbox terminals also live below /api. Without WebSocket proxying
        // enabled, Vite accepts the HTTP routes but drops terminal upgrades.
        ws: true,
        changeOrigin: true,
        // SSE streams can be long-running (thinking + generating + tool calls).
        // Set a generous timeout so the Vite proxy doesn't kill the connection.
        // The frontend's idle-timeout logic handles actual dead connections.
        timeout: 30 * 60 * 1000, // 30 minutes
      },
      "/metrics": {
        target: backendOrigin,
        changeOrigin: true,
      },
      // Voice WebSocket — the backend's /ws/voice endpoint (nginx already
      // proxies /ws in production; this entry makes it work in Docker dev).
      "/ws": {
        target: backendOrigin,
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
