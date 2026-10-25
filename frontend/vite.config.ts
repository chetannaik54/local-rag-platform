/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // In dev, /api is proxied to the FastAPI backend so no CORS or URL config is needed.
    proxy: Object.fromEntries(
      // /api plus the embeddable widget script and its demo page, so the snippet shown in Admin works in dev too
      ["/api", "/widget.js", "/widget-demo"].map((p) => [p, process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8000"]),
    ),
  },
  test: {
    environment: "jsdom",
    globals: true,
    testTimeout: 20000,  // typing-heavy UI tests are slow when the machine is busy (e.g. a local model is running)
    setupFiles: ["./src/test/setup.ts"],
  },
});
