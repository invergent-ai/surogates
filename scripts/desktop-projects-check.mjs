// The web client's projects source (web/src/api/workstream-routes.ts) against a real agent, called
// as Surogate Desktop calls it: through the shell's own checks (desktop/dist/shell/projects.js),
// with each call answered by the source as the page's preload answers it. For the cross-check in
// tests/integration/test_desktop_projects.py; it prints one JSON line, what each call answered.
// Run with node --experimental-strip-types, after the desktop's build.
import { parseArgs } from "node:util";

import { PageProjects } from "../desktop/dist/shell/projects.js";
import { FetchSseEventStream } from "../sdk/agent-chat-react/src/runtime/fetch-sse-stream.ts";
import { workstreamRoutes } from "../web/src/api/workstream-routes.ts";

const { values } = parseArgs({ options: { origin: { type: "string" }, token: { type: "string" }, project: { type: "string" } } });
const { origin, token, project } = values;

// The page's own fetch, signed in: its paths are the agent's.
const signedIn = (input, init = {}) => {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(new URL(String(input), origin), { ...init, headers });
};
const source = workstreamRoutes(signedIn, (url, fetchFn) => new FetchSseEventStream(url, { fetchFn }));

// The page's preload, in this process: what the shell sends, the source answers.
const subscriptions = new Map();
const shell = new PageProjects((message) => {
  if (message.type === "subscribe") {
    subscriptions.set(message.id, source.subscribe(message.projectId, (threadId) => shell.changed(message.id, threadId)));
  } else if (message.type === "unsubscribe") {
    subscriptions.get(message.id)?.();
  } else {
    source[message.method](...message.args).then(
      (ok) => shell.answered(message.id, { ok }),
      (error) => shell.answered(message.id, { error: error.message }),
    );
  }
});

// The project's stream, until it says it is ready.
const heard = [];
const ready = new Promise((resolve) => {
  const stop = shell.subscribe(project, (threadId) => {
    heard.push(threadId);
    stop();
    resolve();
  });
});
const [listed, opened, threads, library, routines] = await Promise.all([
  shell.list(), shell.get(project), shell.threads(project), shell.library(project), shell.routines(project), ready,
]);
const idle = threads.find((row) => row.group === "idle");
const one = await shell.threads(project, idle.id);
const resolved = await shell.resolve(project, idle.id);
const reopened = await shell.reopen(project, idle.id);
const renamed = await shell.update(project, { name: "Q3 report", threadTier: "pro" });
console.log(JSON.stringify({ listed, opened, threads, one, library, routines, heard, resolved, reopened, renamed }));
