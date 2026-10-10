// The file helper. It runs inside the folder's sandbox, so the sandbox's mounts,
// not a path check, bound what it can reach. One JSON request per line on stdin,
// {id, kind, args} or {cancel: id}; one {id, outcome} per line on stdout, after a
// first {ready: true}.

import { createInterface } from "node:readline";

import { edgeRefused } from "./edge.js";
import { recover } from "./land.js";
import { type Context, perform } from "./operations.js";

const { SUROGATE_FOLDER: folder, HOME: home, SUROGATE_AT: at, SUROGATE_COPY: copy, SUROGATE_KEPT: kept, ...rest } = process.env;
if (!folder || !home) {
  process.stderr.write("the file helper needs SUROGATE_FOLDER and HOME\n");
  process.exit(2);
}
// A helper whose folder is a thread's copy is given the path of the folder it is a copy of, here and nowhere else:
// no request names it. One it cannot go by ends the helper, which never works as a chat's in a copy's stead. A
// landing's helper works in the folder itself, and is given none.
if (at !== undefined) {
  const refused = copy !== undefined || kept !== undefined
    ? "the file helper's SUROGATE_AT is for a thread's copy: a landing's helper works in the folder itself, and is given none"
    : edgeRefused(folder, at);
  if (refused !== null) {
    process.stderr.write(`${refused}\n`);
    process.exit(2);
  }
}
// A landing's helper is given the thread's copy and where it keeps the files it replaces: both, or it lands nothing.
const context: Context = {
  folder, home, env: { ...rest, HOME: home }, ...(at !== undefined ? { at } : {}), ...(copy && kept ? { landing: { copy, kept } } : {}),
};
// Before it says it is ready: what an earlier helper's landing left cut short is put back while nothing else looks at
// the folder. A failure here is answered by the first `land` asked, which tries again.
if (context.landing) {
  try {
    recover(context);
  } catch {
    // Said when it is asked.
  }
}
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
