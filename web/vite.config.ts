// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca. See /studio/LICENSE.AGPL-3.0

import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  // In production the agent web app is served at <slug>.<domain>, so the
  // backend resolves the per-request agent from the Host-header subdomain.
  // Behind this dev proxy the Host is rewritten to the target (changeOrigin),
  // so there is no slug to resolve and `agent_runtime_context_dep` 400s with
  // "no agent_id in request". Inject an explicit ?agent_id=<id> into every
  // proxied /api request instead — it is the resolver's highest-precedence
  // source. Set VITE_DEV_AGENT_ID in frontend/.env.local.
  const devAgentId = env.VITE_DEV_AGENT_ID;
  if (!devAgentId) {
    console.warn(
      "\n[vite] VITE_DEV_AGENT_ID is not set — /api requests will 400 with " +
        '"no agent_id in request". Set it in web/.env.local.\n',
    );
  }

  const withAgentId = (p: string): string => {
    const stripped = p.replace(/^\/api/, "");
    if (!devAgentId || /[?&]agent_id=/.test(stripped)) return stripped;
    const sep = stripped.includes("?") ? "&" : "?";
    return `${stripped}${sep}agent_id=${devAgentId}`;
  };

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: "0.0.0.0",
      allowedHosts: true,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          changeOrigin: true,
          ws: true,
          rewrite: withAgentId,
        },
        "/ws": {
          target: "http://127.0.0.1:8000",
          changeOrigin: true,
          ws: true,
        },
      },
    },
    resolve: {
      dedupe: ["react", "react-dom"],
      alias: {
        "@": path.resolve(__dirname, "./src"),
        "@invergent/agent-chat-react": path.resolve(
          __dirname,
          "../sdk/agent-chat-react/src",
        ),
      },
    },
    build: {
      commonjsOptions: {
        include: [/node_modules/, /@dagrejs\/dagre/, /@dagrejs\/graphlib/],
      },
    },
  };
});
