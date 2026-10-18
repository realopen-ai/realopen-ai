import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// STYLING_IMPORTS

export default defineConfig({
  plugins: [react(), ...[] /* STYLING_PLUGINS */],
  server: {
    host: "0.0.0.0",
    port: 6767,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:6969" },
  },
});
