// The file helper. It runs inside the folder's sandbox, so the sandbox's mounts,
// not a path check, bound what it can reach. One JSON request per line on stdin,
// {id, kind, args} or {cancel: id}; one {id, outcome} per line on stdout, after a
// first {ready: true}.

import { lstatSync } from "node:fs";
import { createInterface } from "node:readline";

import { edgeRefused } from "./edge.js";
import { ONLY_RECOVERS } from "./land.js";
import { type Context, perform } from "./operations.js";

const {
  SUROGATE_FOLDER: folder, HOME: home, SUROGATE_AT: at, SUROGATE_COPY: copy, SUROGATE_KEPT: kept,
  SUROGATE_FOLDER_IS: folderIs, SUROGATE_COPY_IS: copyIs, SUROGATE_KEPT_IS: keptIs, ...rest
} = process.env;
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
// Its host checked each folder it is given, and then had the sandbox bind them by their paths: a path can come to
// lead to another folder between the two, by a link put there or a folder moved in. In here the binds are up, and
// what lies at each path is what was bound, for this helper's life: nothing in the sandbox can mount another, and
// nothing outside reaches its mounts. So the helper is told which folder its host found at each, by device and
// inode, and looks before anything else: one that is another ends it here, before a landing's put-back reads a
// record or moves a file, and before it says it is ready. It says so by the folder's name, never by a copy's path.
const found = (path: string | undefined): string | null => {
  try {
    const is = lstatSync(path ?? "");
    return is.isDirectory() ? `${is.dev}:${is.ino}` : null;
  } catch {
    return null;
  }
};
const checked: Array<[is: string | undefined, path: string | undefined, what: string]> = [
  [folderIs, folder, at === undefined ? `the folder ${folder}` : `the copy of ${at} this thread works in`],
  [copyIs, copy, `the thread's copy a landing in ${folder} lands from`],
  [keptIs, kept, `the folder a landing in ${folder} keeps replaced files in`],
];
for (const [is, path, what] of checked) {
  if (is !== undefined && found(path) !== is) {
    process.stderr.write(`${what} is not the folder its host checked: another was at its path as its sandbox was made\n`);
    process.exit(2);
  }
}
// A landing's helper is given the thread's copy and where it keeps the files it replaces: both, or it lands nothing. A
// recovery's is given where they are kept and no copy: it puts back what a landing in the folder cut short, and does
// nothing else. Either puts that back by the first thing it is asked, not before it says it is ready (files/land.ts).
const recovers = kept !== undefined && copy === undefined;
const context: Context = {
  folder, home, env: { ...rest, HOME: home }, ...(at !== undefined ? { at } : {}),
  ...(copy && kept ? { landing: { copy, kept } } : recovers && kept ? { landing: { kept } } : {}),
};
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
  // A recovery's helper is in the user's folder for its put-back alone.
  if (recovers && (kind !== "land" || (args as { action?: unknown }).action !== "recover")) {
    say({ id, outcome: { error: { type: "unsupported", message: ONLY_RECOVERS } } });
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
