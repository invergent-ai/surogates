import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { createServer } = await import(require.resolve("vite"));

const webRoot = fileURLToPath(new URL("../web/", import.meta.url));
let server;
let routeStateModule;

async function loadRouteStateModule() {
  routeStateModule ??= await server.ssrLoadModule(
    "/src/features/chat/chat-route-state.ts",
  );
  return routeStateModule;
}

before(async () => {
  server = await createServer({
    root: webRoot,
    configFile: fileURLToPath(new URL("../web/vite.config.ts", import.meta.url)),
    appType: "custom",
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
});

after(async () => {
  await server?.close();
});

test("the bare /chat route stays a blank new chat even when a session is active", async () => {
  const { getChatRouteState } = await loadRouteStateModule();

  const state = getChatRouteState({
    activeSessionId: "running-session",
    sessionIds: ["running-session"],
    sessionsLoading: false,
    urlSessionId: undefined,
  });

  assert.deepEqual(state, {
    sessionId: null,
    nextActiveSessionId: null,
    redirectTo: null,
  });
});

test("a valid session URL remains the selected chat and syncs the active session", async () => {
  const { getChatRouteState } = await loadRouteStateModule();

  const state = getChatRouteState({
    activeSessionId: null,
    sessionIds: ["selected-session"],
    sessionsLoading: false,
    urlSessionId: "selected-session",
  });

  assert.deepEqual(state, {
    sessionId: "selected-session",
    nextActiveSessionId: "selected-session",
    redirectTo: null,
  });
});
