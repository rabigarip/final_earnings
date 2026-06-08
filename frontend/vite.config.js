import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../static",
    // Don't wipe ../static/ on build — it contains pre-rendered Jabal
    // panel decks under ../static/decks/ that the API serves directly.
    // Vite still overwrites index.html + assets/ which is what we want.
    emptyOutDir: false,
  },
  server: {
    port: 3000,
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } },
  },
});
