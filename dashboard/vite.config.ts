/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// The FastAPI app (started by `cs2bot up`) listens on 127.0.0.1:8000 and serves dashboard/dist.
// During development `npm run dev` serves the app on http://localhost:5173 and proxies /api to it
// (http://localhost:5173 is the API's default CORS origin and its port is on the Host allow-list).
// Override the target with CS2BOT_API_URL (environment variable or dashboard/.env.local).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "CS2BOT_");
  const apiTarget = env.CS2BOT_API_URL || "http://127.0.0.1:8000";
  return {
  plugins: [react()],
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: false },
    },
  },
  preview: { host: "localhost", port: 4173 },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
          charts: ["recharts"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
};
});
