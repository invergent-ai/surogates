import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import type { Mode } from "../src/journal/bindings.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { ProcessHandle } from "../src/guest/processes.js";
import { listFolders, LiveProcesses, stopOperation } from "../src/shell/folders.js";

const FIRST = "44444444-4444-4444-8444-444444444444";
const SECOND = "55555555-5555-4555-8555-555555555555";
const THIRD = "66666666-6666-4666-8666-666666666666";

let base: string;
let journal: OperationJournal;

beforeEach(() => {
  base = mkdtempSync(join(tmpdir(), "folders-"));
  journal = new OperationJournal(join(base, "journal.sqlite"));
});

afterEach(() => {
  journal.close();
  rmSync(base, { recursive: true, force: true });
});

const bind = (root: string, folder: string, mode: Mode, boundAt: number) =>
  journal.bindings.add({ root, nonce: `nonce-${root}`, folder, dev: 1, ino: 1, boot: BOOT_ID, mode, boundAt });

describe("Settings → Folders and permissions", () => {
  it("lists each folder in the order a chat first worked on it, with its chats by their titles and modes", async () => {
    bind(FIRST, "/home/me/notes", "free", 1);
    bind(SECOND, "/home/me/taxes", "ask", 2);
    bind(THIRD, "/home/me/notes", "ask", 3);
    journal.bindings.allowDomain(THIRD, "example.com");
    journal.bindings.allowDomain(THIRD, "[::1]");
    const titles: Record<string, string> = { [FIRST]: "Quarterly report", [SECOND]: "Receipts", [THIRD]: "A chat" };
    expect(await listFolders(journal.bindings, async (root) => titles[root]!, new LiveProcesses())).toEqual([
      {
        folder: "/home/me/notes",
        chats: [
          { root: FIRST, title: "Quarterly report", mode: "free", hosts: [], processes: [] },
          // The hosts its user let it reach, in the order allowed.
          { root: THIRD, title: "A chat", mode: "ask", hosts: ["example.com", "[::1]"], processes: [] },
        ],
      },
      { folder: "/home/me/taxes", chats: [{ root: SECOND, title: "Receipts", mode: "ask", hosts: [], processes: [] }] },
    ]);
  });

  it("is empty while no chat works on a folder of this computer", async () => {
    expect(await listFolders(journal.bindings, async () => "A chat", new LiveProcesses())).toEqual([]);
  });

  it("shows each chat's background processes alive in the VM, as the VM tells each change, and none once its guest has gone", async () => {
    bind(FIRST, "/home/me/notes", "free", 1);
    const live = new LiveProcesses();
    const handle = (id: string, command: string, ended?: ProcessHandle["ended"]): ProcessHandle =>
      ({ id, command, cwd: "/home/me/notes", task_id: null, started_at: 1, ...(ended ? { ended } : {}) });
    live.heard(FIRST, {
      handles: [handle("proc_1", "npm run dev"), handle("proc_2", "make", { exit_code: 0, output: "", note: null })], live: 1,
    });
    const shown = async () => (await listFolders(journal.bindings, async () => "A chat", live))[0]!.chats[0]!.processes;
    // An ended one is no longer there to stop.
    expect(await shown()).toEqual([{ id: "proc_1", command: "npm run dev" }]);
    live.heard(FIRST, { gone: true });
    expect(await shown()).toEqual([]);
  });

  it("stops a process as the agent's own kill does, named as the user's", () => {
    expect(stopOperation(FIRST, "proc_1")).toMatchObject({
      sessionId: FIRST, callingSessionId: FIRST, invocationId: "settings", ordinal: 0, kind: "kill", args: { session_id: "proc_1" },
    });
    expect(stopOperation(FIRST, "proc_1").id).not.toBe(stopOperation(FIRST, "proc_1").id);
  });
});
