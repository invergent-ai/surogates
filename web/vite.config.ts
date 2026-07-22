// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca. See /studio/LICENSE.AGPL-3.0

import fs from "node:fs";
import type { Socket } from "node:net";
import os from "node:os";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

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

  // Firebase's popup helpers are always addressed as https://<authDomain>,
  // and authDomain is this dev server's host — so the dev server must
  // speak TLS. mkcert-minted localhost certs are picked up when present
  // (see ~/.surogates/dev-tls); otherwise plain http still works for
  // everything except Google/GitHub popup sign-in.
  const tlsDir = path.join(os.homedir(), ".surogates", "dev-tls");
  const certFile = path.join(tlsDir, "localhost.pem");
  const keyFile = path.join(tlsDir, "localhost-key.pem");
  const https =
    fs.existsSync(certFile) && fs.existsSync(keyFile)
      ? { cert: fs.readFileSync(certFile), key: fs.readFileSync(keyFile) }
      : undefined;

  // A TLS-only listener answers plain-http requests with an empty reply
  // (Firefox: NS_ERROR_NET_EMPTY_RESPONSE), silently breaking http://
  // bookmarks once the mkcert certs switch the dev server to https. Sniff
  // the first byte of each connection (0x16 = TLS handshake) and redirect
  // plain-http requests to the same URL on https.
  const httpToHttpsRedirect = (): Plugin => ({
    name: "http-to-https-redirect",
    configureServer(server) {
      const httpServer = server.httpServer;
      if (!https || !httpServer) return;
      const tlsListeners = httpServer
        .listeners("connection")
        .slice() as Array<(socket: Socket) => void>;
      httpServer.removeAllListeners("connection");
      httpServer.on("connection", (socket: Socket) => {
        socket.once("data", (chunk: Buffer) => {
          socket.pause();
          socket.unshift(chunk);
          if (chunk[0] === 0x16) {
            for (const listener of tlsListeners) {
              listener.call(httpServer, socket);
            }
            process.nextTick(() => socket.resume());
            return;
          }
          const target = /^[A-Z]+ (\S+) HTTP/.exec(chunk.toString("latin1"));
          const host = /\r\n[Hh]ost: *([^\r\n]+)/.exec(chunk.toString("latin1"));
          socket.end(
            "HTTP/1.1 307 Temporary Redirect\r\n" +
              `Location: https://${host?.[1] ?? "localhost:5174"}${target?.[1] ?? "/"}\r\n` +
              "Connection: close\r\n" +
              "Content-Length: 0\r\n\r\n",
          );
        });
      });
    },
  });

  return {
    plugins: [react(), tailwindcss(), httpToHttpsRedirect()],
    server: {
      host: "0.0.0.0",
      https,
      allowedHosts: true,
      proxy: {
        // Firebase auth helpers, served same-origin so the sign-in
        // popup/iframe channel survives browser storage partitioning
        // (Firefox ETP breaks the cross-origin firebaseapp.com flow).
        // The SPA uses location.host as authDomain in dev to match.
        "/__": {
          target: `https://${
            env.VITE_DEV_FIREBASE_AUTH_DOMAIN || "example.firebaseapp.com"
          }`,
          changeOrigin: true,
          secure: true,
        },
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
