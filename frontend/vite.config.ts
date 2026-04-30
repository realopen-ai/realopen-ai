import { defineConfig } from "vite";
import { fileURLToPath } from "url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

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
        target: "http://backend:8000",
        changeOrigin: true,
      },
      "/metrics": {
        target: "http://backend:8000",
        changeOrigin: true,
      },
    },
  },
});
