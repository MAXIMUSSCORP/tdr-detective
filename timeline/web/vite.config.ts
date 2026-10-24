import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const apiPort = process.env.PORT ?? "4173";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      // changeOrigin rewrites Host to the API's own address, which its DNS-rebinding guard requires.
      "/api": { target: `http://127.0.0.1:${apiPort}`, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
