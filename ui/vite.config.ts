import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `pnpm build` writes the UI into the Python package, so `apm serve` can serve it and the wheel
// ships it. `pnpm dev` proxies to a running `apm serve --dev`.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/apm/server/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 2500,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:4100",
      "/ws": { target: "ws://127.0.0.1:4100", ws: true },
    },
  },
});
