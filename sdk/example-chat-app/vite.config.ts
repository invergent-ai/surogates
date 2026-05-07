import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@invergent/agent-chat-react": fileURLToPath(
        new URL("../agent-chat-react/src/index.ts", import.meta.url),
      ),
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://localhost:8787",
    },
  },
});
