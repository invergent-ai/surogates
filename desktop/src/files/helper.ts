// The file helper. It runs inside the folder's sandbox, so the sandbox's mounts,
// not a path check, bound what it can reach. One JSON request per line on stdin,
// {id, kind, args} or {cancel: id}; one {id, outcome} per line on stdout, after a
// first {ready: true}.

import { createInterface } from "node:readline";

import { type Context, perform } from "./operations.js";

const { SUROGATE_FOLDER: folder, HOME: home, ELECTRON_RUN_AS_NODE: _node, ...rest } = process.env;
if (!folder || !home) {
  process.stderr.write("the file helper needs SUROGATE_FOLDER and HOME\n");
  process.exit(2);
}
const context: Context = { folder, home, env: { ...rest, HOME: home } };
const running = new Map<string, AbortController>();
const say = (line: unknown) => process.stdout.write(`${JSON.stringify(line)}\n`);

createInterface({ input: process.stdin }).on("line", (line) => {
  let request: { id?: unknown; kind?: unknown; args?: unknown; cancel?: unknown } | null;
  try {
    request = JSON.parse(line) as typeof request;
  } catch {
    return;
  }
  if (typeof request !== "object" || request === null) return;
  if (typeof request.cancel === "string") {
    running.get(request.cancel)?.abort();
    return;
  }
  const { id, kind, args } = request;
  if (typeof id !== "string") return;
  if (typeof kind !== "string" || typeof args !== "object" || args === null) {
    say({ id, outcome: { error: { type: "value", message: "malformed request" } } });
    return;
  }
  const controller = new AbortController();
  running.set(id, controller);
  void perform(kind, args as Record<string, unknown>, context, controller.signal).then((outcome) => {
    running.delete(id);
    say({ id, outcome });
  });
});
process.stdin.on("end", () => process.exit(0));
say({ ready: true });
