// The web client's projects source (web/src/api/workstream-routes.ts) against a real agent, called
// as Surogate Desktop calls it: through the shell's own checks (desktop/dist/shell/projects.js),
// with each call answered by the source as the page's preload answers it. For the cross-check in
// tests/integration/test_desktop_projects.py; it prints one JSON line, what each call answered.
// Run with node --experimental-strip-types, after the desktop's build.
import { parseArgs } from "node:util";

import { PageProjects } from "../desktop/dist/shell/projects.js";
import { FetchSseEventStream } from "../sdk/agent-chat-react/src/runtime/fetch-sse-stream.ts";
import { workstreamRoutes } from "../web/src/api/workstream-routes.ts";

const { values } = parseArgs({
  options: { origin: { type: "string" }, token: { type: "string" }, project: { type: "string" }, path: { type: "string" } },
});
const { origin, token, project, path } = values;

// The page's own fetch, signed in: its paths are the agent's.
const signedIn = (input, init = {}) => {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(new URL(String(input), origin), { ...init, headers });
};
// A version opened is handed here to save: what the check prints of it is its name, what it is saved as, and its size.
const saved = [];
const source = workstreamRoutes(signedIn, (url, fetchFn) => new FetchSseEventStream(url, { fetchFn }), (data, name) => {
  saved.push({ name, type: data.type, size: data.size });
});

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
// With a file: its History, and that of a file the project never held; its oldest version opened; the
// project's deleted files; and its oldest version restored, with its History read again.
const history = {};
if (path) {
  history.versions = await shell.history(project, path, { kind: "cloud" });
  history.none = await shell.history(project, `no-${path}`, { kind: "cloud" });
  history.answered = await shell.openVersion(project, { versionId: history.versions.at(-1).id, path }) ?? null;
  history.opened = saved;
  history.deleted = await shell.deleted(project);
  history.restored = await shell.restore(project, { versionId: history.versions.at(-1).id, path });
  history.after = await shell.history(project, path, { kind: "cloud" });
}
console.log(JSON.stringify({ listed, opened, threads, one, library, routines, heard, resolved, reopened, renamed, history }));
