import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const proxyTarget = (globalThis as typeof globalThis & {
  process?: { env?: Record<string, string | undefined> };
}).process?.env?.IDP_API_PROXY_TARGET;

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: proxyTarget ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: { environment: "jsdom", setupFiles: "./src/test-setup.ts" },
});

