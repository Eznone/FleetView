import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The daemon serves `dist/` in production. In development Vite serves the app
// and proxies the API to the daemon, so the browser still sees one origin --
// which matters, because the daemon's LoopbackGuard checks Host and Origin and
// a cross-origin dev server would be refused exactly as an attacker would be.
const DAEMON = process.env.FLEETVIEW_UI_ORIGIN ?? "http://127.0.0.1:8420";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/v1": { target: DAEMON, changeOrigin: true },
      "/ws": { target: DAEMON, changeOrigin: true, ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
