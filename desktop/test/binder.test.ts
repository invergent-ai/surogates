import {
  existsSync, linkSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalPrompts, ApprovalRequest } from "../src/binding/approvals.js";
import {
  ALREADY_BOUND, Binder, type BinderOptions, type FolderPrompts, type FolderSheet, NOT_RECORDED, type Prepared,
} from "../src/binding/binder.js";
import { BOOT_ID } from "../src/binding/folder.js";
import { connectDevice } from "../src/device.js";
import { NOT_BOUND } from "../src/hosts/tool-hosts.js";
import type { Mode } from "../src/journal/bindings.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { DeviceLink } from "../src/link/client.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import type { Executor } from "../src/operations/runner.js";
import { FakeLinkServer } from "./fake-server.js";

const ROOT = "44444444-4444-4444-8444-444444444444";
const OTHER = "55555555-5555-4555-8555-555555555555";
const WINDOW = "window-1";

// The user, as the dialog and the sheet meet them: each dialog picks the next of
// picks, each sheet gets the next of answers; past the end, both cancel.
class User implements FolderPrompts {
  readonly sheets: FolderSheet[] = [];
  readonly dialogs: string[] = [];

  constructor(readonly picks: Array<string | null> = [], readonly answers: Array<{ mode: Mode } | "change" | null> = []) {}

  pickFolder(startIn: string): Promise<string | null> {
    this.dialogs.push(startIn);
    return Promise.resolve(this.picks.shift() ?? null);
  }

  confirmFolder(sheet: FolderSheet): Promise<{ mode: Mode } | "change" | null> {
    this.sheets.push(sheet);
    return Promise.resolve(this.answers.shift() ?? null);
  }
}

// The tool hosts, as far as the binder sees them.
class Hosts implements Executor {
  readonly ran: Operation[] = [];
  ended = 0;

  run(operation: Operation): Promise<Outcome> {
    this.ran.push(operation);
    return Promise.resolve({ ok: `ran ${operation.kind}` });
  }

  end(): Promise<void> {
    this.ended += 1;
    return Promise.resolve();
  }
}

let base: string;
let notes: string;
let path: string;
let journal: OperationJournal;
let journals: OperationJournal[];
let hosts: Hosts;
let server: FakeLinkServer;
let links: DeviceLink[];
let url: string | undefined;

function open(): OperationJournal {
  const opened = new OperationJournal(path);
  journals.push(opened);
  return opened;
}

// The user at the approval prompts: allows each operation, and never lets a chat work freely.
const allowing: ApprovalPrompts = {
  approve: () => Promise.resolve("allow"),
  confirmFreeMode: () => Promise.resolve(false),
};

function binder(user: User, overrides: Partial<BinderOptions> = {}): Binder {
  return new Binder({
    bindings: journal.bindings,
    prompts: user,
    guards: { home: join(base, "home"), dataDir: join(base, "data"), appDirs: [join(base, "app")] },
    agent: "Research assistant",
    hosts,
    approvalPrompts: allowing,
    ...overrides,
  });
}

const never = () => new AbortController().signal;

// The user confirms *folder* through the dialog and the sheet.
async function confirmed(user: User, chooser: Binder, folder = notes, mode: Mode = "free"): Promise<Prepared> {
  user.picks.push(folder);
  user.answers.push({ mode });
  const ready = await chooser.prepareFolder("pick", WINDOW, never());
  if (!ready) throw new Error("the folder was not confirmed");
  return ready;
}

const bindOp = (root: string, ready: { folder: string; nonce: string }, changes: Partial<Operation> = {}): Operation => ({
  id: `bind-${root}`, sessionId: root, callingSessionId: root, invocationId: "bind", ordinal: 0, kind: "bind",
  args: { folder: ready.folder, nonce: ready.nonce }, digest: `digest-${root}`, ...changes,
});

const frame = (operation: Operation): Record<string, unknown> => ({
  type: "op", id: operation.id, session_id: operation.sessionId, calling_session_id: operation.callingSessionId,
  invocation_id: operation.invocationId, ordinal: operation.ordinal, kind: operation.kind, args: operation.args,
  digest: operation.digest,
});

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "binder-")));
  notes = join(base, "notes");
  path = join(base, "journal.sqlite");
  for (const dir of ["home", "notes", "other"]) mkdirSync(join(base, dir));
  journals = [];
  journal = open();
  hosts = new Hosts();
  server = new FakeLinkServer();
  links = [];
  url = undefined;
});

afterEach(async () => {
  for (const link of links) await link.stop();
  await server.stop();
  for (const opened of journals) opened.close();
  rmSync(base, { recursive: true, force: true });
});

describe("preparing a new chat's folder", () => {
  it("offers a new folder of its own under ~/Surogate/<agent> when nothing was bound yet, and keeps the mode chosen there", async () => {
    const user = new User([], [{ mode: "ask" }]);
    const ready = await binder(user).prepareFolder("last", WINDOW, never());
    const made = user.sheets[0]?.folder ?? "";
    expect(made).toMatch(new RegExp(`^${join(base, "home", "Surogate", "Research-assistant")}/\\d{4}-\\d{2}-\\d{2}$`));
    expect(user.dialogs).toEqual([]);
    expect(user.sheets).toEqual([{ agent: "Research assistant", folder: made, mode: "free", links: null, refusal: null }]);
    expect(ready).toEqual({
      folder: made, mode: "ask", nonce: expect.stringMatching(/^[A-Za-z0-9_-]{16,128}$/), token: expect.any(String),
    });
    expect(ready?.token).not.toBe(ready?.nonce);
    expect(statSync(made).isDirectory()).toBe(true);
    // Another chat the same day, before the first is bound, gets a folder of its own.
    const second = new User([], [{ mode: "free" }]);
    expect((await binder(second).prepareFolder("last", WINDOW, never()))?.folder).toBe(`${made} 2`);
  });

  it("removes the new folder again when the user cancels, or takes another", async () => {
    const cancelling = new User([], [null]);
    expect(await binder(cancelling).prepareFolder("last", WINDOW, never())).toBeNull();
    const made = cancelling.sheets[0]?.folder ?? "";
    expect(existsSync(made)).toBe(false);
    const changing = new User([notes], ["change", { mode: "free" }]);
    expect((await binder(changing).prepareFolder("last", WINDOW, never()))?.folder).toBe(notes);
    expect(changing.dialogs).toEqual([made]);
    expect(existsSync(made)).toBe(false);
    // A sheet that fails leaves none behind either.
    const failing = new User();
    failing.confirmFolder = (sheet) => {
      failing.sheets.push(sheet);
      return Promise.reject(new Error("Surogate has no window to ask in"));
    };
    await expect(binder(failing).prepareFolder("last", WINDOW, never())).rejects.toThrow("Surogate has no window to ask in");
    expect(failing.sheets[0]?.folder).toBe(made);
    expect(existsSync(made)).toBe(false);
    // One the user put something in stays.
    const filling = new User([], [null]);
    filling.confirmFolder = (sheet) => {
      filling.sheets.push(sheet);
      writeFileSync(join(sheet.folder, "draft.txt"), "kept");
      return Promise.resolve(null);
    };
    await binder(filling).prepareFolder("last", WINDOW, never());
    expect(existsSync(join(made, "draft.txt"))).toBe(true);
  });

  it("shows the last folder bound without a dialog, and a new folder once that one has gone", async () => {
    journal.bindings.add({ root: OTHER, nonce: "n".repeat(16), folder: notes, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    const user = new User([], [{ mode: "free" }]);
    expect((await binder(user).prepareFolder("last", WINDOW, never()))?.folder).toBe(notes);
    expect(user.dialogs).toEqual([]);
    rmSync(notes, { recursive: true });
    const again = new User([], [{ mode: "free" }]);
    expect((await binder(again).prepareFolder("last", WINDOW, never()))?.folder).toMatch(/Surogate\/Research-assistant\/\d{4}-\d{2}-\d{2}$/);
    expect(again.dialogs).toEqual([]);
  });

  it.each([
    ["../../escape", "escape"],
    ["[::1]:8080", "1-8080"],
    ["a/b", "a-b"],
    [".hidden", "hidden"],
  ])("makes the new folder for the agent %s under ~/Surogate/%s", async (agent, name) => {
    const user = new User([], [null]);
    await binder(user, { agent }).prepareFolder("last", WINDOW, never());
    const made = user.sheets[0]?.folder ?? "";
    expect(made.startsWith(`${join(base, "home", "Surogate", name)}/`)).toBe(true);
    expect(made.split("/").at(-1)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(user.sheets[0]?.refusal).toBeNull();
  });

  it("opens the dialog where the last folder was when no new folder can be made", async () => {
    writeFileSync(join(base, "home", "Surogate"), "not a folder");
    const user = new User([notes], [{ mode: "free" }]);
    expect((await binder(user).prepareFolder("last", WINDOW, never()))?.folder).toBe(notes);
    expect(user.dialogs).toEqual([join(base, "home")]);
    // Nor for an agent whose name makes no folder name.
    rmSync(join(base, "home", "Surogate"));
    const unnamed = new User([notes], [{ mode: "free" }]);
    expect((await binder(unnamed, { agent: ".." }).prepareFolder("last", WINDOW, never()))?.folder).toBe(notes);
    expect(unnamed.dialogs).toEqual([join(base, "home")]);
    // Where the last folder bound was, once that one has gone.
    const other = join(base, "other");
    journal.bindings.add({ root: OTHER, nonce: "n".repeat(16), folder: other, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    rmSync(other, { recursive: true });
    writeFileSync(join(base, "home", "Surogate"), "not a folder");
    const gone = new User([notes], [{ mode: "free" }]);
    expect((await binder(gone).prepareFolder("last", WINDOW, never()))?.folder).toBe(notes);
    expect(gone.dialogs).toEqual([other]);
  });

  it("opens the dialog, and leaves no folder of its own, when the new one would be a folder no chat may have", async () => {
    mkdirSync(join(base, "data"));
    symlinkSync(join(base, "data"), join(base, "home", "Surogate"));
    const user = new User([notes], [{ mode: "free" }]);
    expect((await binder(user).prepareFolder("last", WINDOW, never()))?.folder).toBe(notes);
    expect(user.dialogs).toEqual([join(base, "home")]);
    expect(user.sheets.map((sheet) => sheet.folder)).toEqual([notes]);
    expect(readdirSync(join(base, "data", "Research-assistant"))).toEqual([]);
  });

  it("shows the files in the folder that are linked from elsewhere", async () => {
    writeFileSync(join(base, "o.txt"), "o");
    linkSync(join(base, "o.txt"), join(notes, "linked.txt"));
    const user = new User([notes], [{ mode: "free" }]);
    await binder(user).prepareFolder("pick", WINDOW, never());
    expect(user.sheets[0]?.links).toEqual({ count: 1, examples: ["linked.txt"], complete: true });
  });

  it("shows why a folder cannot be used, and accepts nothing until another is chosen", async () => {
    const user = new User([join(base, "home"), notes], ["change", { mode: "free" }]);
    expect((await binder(user).prepareFolder("pick", WINDOW, never()))?.folder).toBe(notes);
    expect(user.sheets[0]).toMatchObject({ folder: join(base, "home"), links: null, refusal: expect.stringMatching(/home folder/) });
    expect(user.dialogs).toEqual([join(base, "home"), join(base, "home")]);
    const accepting = new User([join(base, "home")], [{ mode: "free" }]);
    expect(await binder(accepting).prepareFolder("pick", WINDOW, never())).toBeNull();
  });

  it("goes back to the sheet when the dialog that Change opened is cancelled", async () => {
    const user = new User([notes, null], ["change", { mode: "free" }]);
    expect((await binder(user).prepareFolder("pick", WINDOW, never()))?.folder).toBe(notes);
    expect(user.sheets.map((sheet) => sheet.folder)).toEqual([notes, notes]);
  });

  it("is nothing when the user cancels the dialog or the sheet, or the page goes away", async () => {
    expect(await binder(new User([null])).prepareFolder("pick", WINDOW, never())).toBeNull();
    expect(await binder(new User([notes], [null])).prepareFolder("pick", WINDOW, never())).toBeNull();
    const gone = new AbortController();
    gone.abort();
    expect(await binder(new User([notes], [{ mode: "free" }])).prepareFolder("pick", WINDOW, gone.signal)).toBeNull();
  });

  it("opens no dialog and no sheet for a page that has already gone", async () => {
    const gone = new AbortController();
    gone.abort();
    const user = new User([notes], [{ mode: "free" }]);
    expect(await binder(user).prepareFolder("pick", WINDOW, gone.signal)).toBeNull();
    journal.bindings.add({ root: OTHER, nonce: "n".repeat(16), folder: notes, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    expect(await binder(user).prepareFolder("last", WINDOW, gone.signal)).toBeNull();
    expect([user.dialogs, user.sheets]).toEqual([[], []]);
  });

  it("opens no sheet for a page that went away while the folder was scanned", async () => {
    const gone = new AbortController();
    const user = new User([], [{ mode: "free" }]);
    user.pickFolder = (startIn) => {
      user.dialogs.push(startIn);
      // After the dialog answers, while its folder is scanned.
      setImmediate(() => gone.abort());
      return Promise.resolve(notes);
    };
    expect(await binder(user).prepareFolder("pick", WINDOW, gone.signal)).toBeNull();
    expect(user.sheets).toEqual([]);
  });

  it("is nothing when the page goes away while the sheet is open, even if the sheet is then accepted", async () => {
    const gone = new AbortController();
    const user = new User([notes]);
    user.confirmFolder = (sheet) => {
      user.sheets.push(sheet);
      gone.abort();
      return Promise.resolve({ mode: "free" });
    };
    expect(await binder(user).prepareFolder("pick", WINDOW, gone.signal)).toBeNull();
    expect(user.sheets).toHaveLength(1);
  });
});

describe("a chat's bind operation", () => {
  it("binds the chat to the folder confirmed under its nonce, with that folder's identity and mode", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser, notes, "ask");
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual({ ok: null });
    const { dev, ino } = statSync(notes);
    expect(journal.bindings.get(ROOT)).toEqual({
      root: ROOT, nonce: ready.nonce, folder: notes, dev, ino, boot: BOOT_ID, mode: "ask", boundAt: expect.any(Number),
    });
  });

  it("is refused for a nonce this computer never gave out, or one already used, and binds nothing", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    expect(await chooser.admit(bindOp(ROOT, { ...ready, nonce: "x".repeat(32) }), never())).toEqual(NOT_BOUND);
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual({ ok: null });
    expect(await chooser.admit(bindOp(OTHER, ready), never())).toEqual(NOT_BOUND);
    expect(journal.bindings.get(OTHER)).toBeUndefined();
  });

  it("is refused, and uses up its confirmation, when it names another folder", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    expect(await chooser.admit(bindOp(ROOT, { ...ready, folder: `${notes}/` }), never())).toEqual(NOT_BOUND);
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual(NOT_BOUND);
    expect(journal.bindings.get(ROOT)).toBeUndefined();
    await expect(chooser.bindSession(ROOT, ready.token, WINDOW)).rejects.toThrow("The server named another folder for this chat");
  });

  it.each([
    ["a session under the chat", { callingSessionId: OTHER }],
    ["another invocation", { invocationId: "12:call_1" }],
    ["a later ordinal", { ordinal: 1 }],
    ["arguments that are not text", { args: { folder: 1, nonce: 2 } }],
  ])("is refused when it comes from %s", async (_name, changes) => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    expect(await chooser.admit(bindOp(ROOT, ready, changes), never())).toEqual(NOT_BOUND);
    expect(journal.bindings.get(ROOT)).toBeUndefined();
  });

  it("is refused once its confirmation has expired, and the page is told so once", async () => {
    const user = new User();
    const chooser = binder(user, { preparedMs: 20 });
    const ready = await confirmed(user, chooser);
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual(NOT_BOUND);
    await expect(chooser.bindSession(ROOT, ready.token, WINDOW))
      .rejects.toThrow("This folder's confirmation expired before its chat was created");
    await expect(chooser.bindSession(ROOT, ready.token, WINDOW)).rejects.toThrow("This folder was not confirmed in this window");
  });

  it.each([
    ["removed", () => rmSync(notes, { recursive: true })],
    ["replaced", () => {
      renameSync(notes, join(base, "moved"));
      mkdirSync(notes);
    }],
  ])("reads no file: a folder %s since it was confirmed is bound, with the identity confirmed", async (_name, change) => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    const { dev, ino } = statSync(notes);
    change();
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual({ ok: null });
    expect(journal.bindings.get(ROOT)).toMatchObject({ folder: notes, dev, ino });
  });

  it("is refused when the binding cannot be recorded, and the page and onError hear why at once", async () => {
    const user = new User();
    const failures: unknown[] = [];
    const chooser = binder(user, { onError: (error) => failures.push(error) });
    const ready = await confirmed(user, chooser);
    const full = new Error("database or disk is full");
    vi.spyOn(journal.bindings, "add").mockImplementation(() => {
      throw full;
    });
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual(NOT_RECORDED);
    await expect(chooser.bindSession(ROOT, ready.token, WINDOW)).rejects.toBe(full);
    expect(failures).toEqual([full]);
    expect(journal.bindings.get(ROOT)).toBeUndefined();
  });

  it("is answered from the binding when it comes again after a restart, and refused once the chat is bound otherwise", async () => {
    const nonce = "n".repeat(32);
    journal.bindings.add({ root: ROOT, nonce, folder: notes, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    // A restarted app has no confirmations; the binding is in the journal.
    const restarted = binder(new User());
    expect(await restarted.admit(bindOp(ROOT, { folder: notes, nonce }), never())).toEqual({ ok: null });
    expect(await restarted.admit(bindOp(ROOT, { folder: notes, nonce: "y".repeat(32) }), never())).toEqual(ALREADY_BOUND);
    expect(await restarted.admit(bindOp(ROOT, { folder: join(base, "other"), nonce }), never())).toEqual(ALREADY_BOUND);
  });

  it("never answers with a message that ends in a full stop", () => {
    for (const outcome of [NOT_BOUND, ALREADY_BOUND, NOT_RECORDED]) {
      expect("error" in outcome && outcome.error.message.endsWith(".")).toBe(false);
    }
  });

  it("asks the approvals about every other operation before the tool hosts run it", async () => {
    journal.bindings.add({ root: ROOT, nonce: "n".repeat(16), folder: notes, dev: 1, ino: 1, boot: BOOT_ID, mode: "ask", boundAt: 1 });
    const asked: string[] = [];
    const denying: ApprovalPrompts = {
      approve: (request) => {
        asked.push(request.kind);
        return Promise.resolve("deny");
      },
      confirmFreeMode: () => Promise.resolve(false),
    };
    const chooser = binder(new User(), { approvalPrompts: denying });
    const run = {
      ...bindOp(ROOT, { folder: notes, nonce: "n" }), kind: "run", invocationId: "1:c", ordinal: 1,
      args: { command: "ls", workdir: null, timeout: 10 },
    };
    expect(await chooser.admit(run, never())).toEqual({
      error: { type: "sandbox", message: "The user denied this command on this computer" },
    });
    expect(asked).toEqual(["command"]);
  });

  it("tells the approvals the command each background process runs, as it starts", async () => {
    const asked: ApprovalRequest[] = [];
    hosts.run = (operation) => Promise.resolve(operation.kind === "start" ? { ok: { session_id: "proc_1", pid: 7 } } : { ok: null });
    const chooser = binder(new User(), {
      approvalPrompts: {
        approve: (request) => {
          asked.push(request);
          return Promise.resolve("deny");
        },
        confirmFreeMode: () => Promise.resolve(false),
      },
    });
    journal.bindings.add({ root: ROOT, nonce: "n".repeat(16), folder: notes, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    const start: Operation = {
      id: "s", sessionId: ROOT, callingSessionId: ROOT, invocationId: "1:c", ordinal: 1, kind: "start", args: { command: "npm run dev" }, digest: "d",
    };
    expect(await chooser.run(start, never())).toEqual({ ok: { session_id: "proc_1", pid: 7 } });
    journal.bindings.setMode(ROOT, "ask");
    await chooser.admit({ ...start, id: "i", ordinal: 2, kind: "write_stdin", args: { session_id: "proc_1", data: "q" } }, never());
    expect(asked).toMatchObject([{ kind: "input", process: "proc_1", command: "npm run dev" }]);
  });

  it("is the binder's own: every other operation goes to the tool hosts, and so does the end of access", async () => {
    const chooser = binder(new User());
    const op = { ...bindOp(ROOT, { folder: notes, nonce: "n" }), kind: "resolve", invocationId: "1:c", ordinal: 1 };
    expect(await chooser.admit(op, never())).toBeNull();
    expect(await chooser.run(op, never())).toEqual({ ok: "ran resolve" });
    await chooser.end();
    expect([hosts.ran, hosts.ended]).toEqual([[op], 1]);
  });
});

describe("waiting for a chat's binding", () => {
  it("resolves once the server has recorded the bind, whether the page asks before or after it arrives", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    let bound: unknown = null;
    void chooser.bindSession(ROOT, ready.token, WINDOW).then((binding) => {
      bound = binding;
    });
    await chooser.admit(bindOp(ROOT, ready), never());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(bound).toBeNull();
    chooser.acknowledged(`bind-${ROOT}`);
    await vi.waitFor(() => expect(bound).toMatchObject({ root: ROOT, folder: notes }));
    expect(await chooser.bindSession(ROOT, ready.token, WINDOW)).toEqual(bound);
    const later = await confirmed(user, chooser, join(base, "other"));
    await chooser.admit(bindOp(OTHER, later), never());
    chooser.acknowledged(`bind-${OTHER}`);
    expect(await chooser.bindSession(OTHER, later.token, WINDOW)).toMatchObject({ root: OTHER });
  });

  it("changes nothing for an acknowledgement of no bind answered here, or a repeated one", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    let bound: unknown = null;
    void chooser.bindSession(ROOT, ready.token, WINDOW).then((binding) => {
      bound = binding;
    });
    chooser.acknowledged("work");
    chooser.acknowledged(`bind-${ROOT}`);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(bound).toBeNull();
    await chooser.admit(bindOp(ROOT, ready), never());
    chooser.acknowledged(`bind-${ROOT}`);
    chooser.acknowledged(`bind-${ROOT}`);
    await vi.waitFor(() => expect(bound).toMatchObject({ root: ROOT, folder: notes }));
  });

  it("is refused for another window, an unknown token, and another chat", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    await chooser.admit(bindOp(ROOT, ready), never());
    chooser.acknowledged(`bind-${ROOT}`);
    await expect(chooser.bindSession(ROOT, ready.token, "window-2")).rejects.toThrow("not confirmed in this window");
    await expect(chooser.bindSession(ROOT, "unknown", WINDOW)).rejects.toThrow("not confirmed in this window");
    await expect(chooser.bindSession(OTHER, ready.token, WINDOW)).rejects.toThrow("This folder was confirmed for another chat");
  });

  it("is refused once this computer's access ended before the server recorded the bind", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    await chooser.admit(bindOp(ROOT, ready), never());
    const waiting = chooser.bindSession(ROOT, ready.token, WINDOW);
    await chooser.end();
    await expect(waiting).rejects.toThrow("This computer's access to the agent ended before its chat's folder was recorded");
    await expect(chooser.bindSession(ROOT, ready.token, WINDOW)).rejects.toThrow(/access to the agent ended/);
    expect(hosts.ended).toBe(1);
  });

  it("is refused once the confirmation expired before its chat's bind came", async () => {
    const user = new User();
    const chooser = binder(user, { preparedMs: 20 });
    const ready = await confirmed(user, chooser);
    await expect(chooser.bindSession(ROOT, ready.token, WINDOW)).rejects.toThrow(/expired/);
  });
});

describe("dropping a confirmed folder", () => {
  it("refuses the bind of a chat that was never created, and tells the page waiting for it", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    const waiting = chooser.bindSession(ROOT, ready.token, WINDOW);
    chooser.cancelPrepared(ready.token, WINDOW);
    await expect(waiting).rejects.toThrow("This folder's confirmation was dropped before its chat was created");
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual(NOT_BOUND);
    expect(journal.bindings.get(ROOT)).toBeUndefined();
    // Dropped once: a repeat changes nothing.
    chooser.cancelPrepared(ready.token, WINDOW);
  });

  it("removes a new folder of its own that was taken, once its confirmation is dropped or expires unbound", async () => {
    const user = new User([], [{ mode: "free" }, { mode: "free" }]);
    const dropping = binder(user);
    const dropped = await dropping.prepareFolder("last", WINDOW, never());
    dropping.cancelPrepared(dropped!.token, WINDOW);
    expect(existsSync(dropped!.folder)).toBe(false);
    const expiring = binder(user, { preparedMs: 20 });
    const expired = await expiring.prepareFolder("last", WINDOW, never());
    expect(existsSync(expired!.folder)).toBe(true);
    await vi.waitFor(() => expect(existsSync(expired!.folder)).toBe(false));
  });

  // The user takes the new folder of its own the sheet offers.
  async function takenNew(user: User, chooser: Binder): Promise<Prepared> {
    user.answers.push({ mode: "free" });
    const ready = await chooser.prepareFolder("last", WINDOW, never());
    if (!ready) throw new Error("the new folder was not taken");
    return ready;
  }

  const past = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

  it("keeps the same day's folder made again for the next chat, which binds it, once the first's confirmation is dropped", async () => {
    const user = new User();
    const chooser = binder(user, { preparedMs: 200 });
    const first = await takenNew(user, chooser);
    chooser.cancelPrepared(first.token, WINDOW);
    expect(existsSync(first.folder)).toBe(false);
    const next = await takenNew(user, chooser);
    expect(next.folder).toBe(first.folder);
    expect(await chooser.admit(bindOp(ROOT, next), never())).toEqual({ ok: null });
    // Past the first's expiry.
    await past(400);
    expect(existsSync(next.folder)).toBe(true);
  });

  it("keeps the same day's folder made again for the next chat, which binds it, when the first's expired confirmation is dropped late", async () => {
    const user = new User();
    const chooser = binder(user, { preparedMs: 200 });
    const first = await takenNew(user, chooser);
    await vi.waitFor(() => expect(existsSync(first.folder)).toBe(false));
    const next = await takenNew(user, chooser);
    expect(next.folder).toBe(first.folder);
    expect(await chooser.admit(bindOp(ROOT, next), never())).toEqual({ ok: null });
    chooser.cancelPrepared(first.token, WINDOW);
    expect(existsSync(next.folder)).toBe(true);
  });

  it("keeps a new folder of its own that another chat's confirmation or binding holds", async () => {
    const user = new User();
    const chooser = binder(user, { preparedMs: 200 });
    // Picked for another chat while its own is open, then its own is dropped.
    const first = await takenNew(user, chooser);
    const picked = await confirmed(user, chooser, first.folder);
    chooser.cancelPrepared(first.token, WINDOW);
    expect(existsSync(first.folder)).toBe(true);
    // Picked for another chat, which binds it, then its own expires.
    const second = await takenNew(user, chooser);
    expect(second.folder).toBe(`${first.folder} 2`);
    const bound = await confirmed(user, chooser, second.folder);
    expect(await chooser.admit(bindOp(ROOT, bound), never())).toEqual({ ok: null });
    await past(400);
    expect(existsSync(second.folder)).toBe(true);
    // Nor did the other chat's confirmation, expiring, remove a folder not made for it.
    expect(existsSync(picked.folder)).toBe(true);
  });

  it("drops nothing for another window, an unknown token, or a chat already bound", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    chooser.cancelPrepared(ready.token, "window-2");
    chooser.cancelPrepared("x".repeat(43), WINDOW);
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual({ ok: null });
    chooser.cancelPrepared(ready.token, WINDOW);
    expect(journal.bindings.get(ROOT)?.folder).toBe(notes);
  });
});

describe("what the page may know of a chat's folder", () => {
  it("is the folder and the mode of a chat bound here, as its mode changes, and nothing for a chat with none", async () => {
    const user = new User();
    const chooser = binder(user);
    expect(chooser.bindingOf(ROOT)).toBeNull();
    const ready = await confirmed(user, chooser, notes, "ask");
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual({ ok: null });
    expect(chooser.bindingOf(ROOT)).toEqual({ folder: notes, mode: "ask" });
    journal.bindings.setMode(ROOT, "free");
    expect(chooser.bindingOf(ROOT)).toEqual({ folder: notes, mode: "free" });
    expect(chooser.bindingOf(OTHER)).toBeNull();
  });

  it("tells whoever watches of each chat bound and each change of its mode, until they stop, whatever one of them throws", async () => {
    const heard: string[] = [];
    journal.bindings.watch(() => {
      throw new Error("a listener that fails");
    });
    const stop = journal.bindings.watch((root) => heard.push(root));
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    // Recorded all the same: a listener's failure is not the binding's.
    expect(await chooser.admit(bindOp(ROOT, ready), never())).toEqual({ ok: null });
    chooser.approvals.setMode(ROOT, "ask");
    // A write that changes nothing tells nothing: a chat with no folder here, a mode it has already.
    journal.bindings.setMode(OTHER, "ask");
    journal.bindings.setMode(ROOT, "ask");
    stop();
    journal.bindings.setMode(ROOT, "free");
    expect(heard).toEqual([ROOT, ROOT]);
  });

  it("shows a chat's folder only while it is still the one its user confirmed", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    await chooser.admit(bindOp(ROOT, ready), never());
    expect(await chooser.folderToShow(ROOT)).toBe(notes);
    await expect(chooser.folderToShow(OTHER)).rejects.toThrow("This chat has no folder on this computer");
    // Another folder at its path, a link to another folder, a file: none is the chat's.
    renameSync(notes, join(base, "moved"));
    mkdirSync(notes);
    const replaced = `The folder ${notes} was replaced after it was confirmed for this chat`;
    await expect(chooser.folderToShow(ROOT)).rejects.toThrow(replaced);
    rmSync(notes, { recursive: true });
    symlinkSync(join(base, "other"), notes);
    await expect(chooser.folderToShow(ROOT)).rejects.toThrow(replaced);
    rmSync(notes);
    writeFileSync(notes, "");
    await expect(chooser.folderToShow(ROOT)).rejects.toThrow(replaced);
    rmSync(notes);
    await expect(chooser.folderToShow(ROOT)).rejects.toThrow(`The folder ${notes} is not there`);
  });

  it("refuses a file that took the folder's inode, as a deleted folder's can be given again", async () => {
    // On ext4 a file made where the folder was gets a new inode, which the inode compare already
    // refuses: a look that answers a file with the binding's own inode pins the folder check.
    const user = new User();
    const { dev, ino } = statSync(notes);
    const chooser = binder(user, { look: async () => ({ isDirectory: () => false, dev, ino }) });
    await chooser.admit(bindOp(ROOT, await confirmed(user, chooser)), never());
    await expect(chooser.folderToShow(ROOT)).rejects.toThrow(`The folder ${notes} was replaced after it was confirmed for this chat`);
  });

  it("gives up on a folder that does not answer, and looks again only once that look has ended", async () => {
    const user = new User();
    const { promise: answered, resolve: answer } = Promise.withResolvers<{ isDirectory(): boolean; dev: number; ino: number }>();
    const chooser = binder(user, { look: () => answered, lookMs: 50 });
    await chooser.admit(bindOp(ROOT, await confirmed(user, chooser)), never());
    // A dead network or FUSE mount: its look holds a thread until it returns, so one look at a time.
    await expect(chooser.folderToShow(ROOT)).rejects.toThrow(`The folder ${notes} did not answer within 0.05 s`);
    await expect(chooser.folderToShow(ROOT)).rejects.toThrow(`Surogate is still looking for ${notes}`);
    answer(statSync(notes));
    await answered;
    await new Promise((resolve) => setImmediate(resolve));
    expect(await chooser.folderToShow(ROOT)).toBe(notes);
  });
});

describe("binding over the link", () => {
  async function connect(executor: Executor, onError: (error: unknown) => void = () => {}): Promise<DeviceLink> {
    url ??= await server.start();
    const device = connectDevice({ url, token: "surg_dev_test", journal, executor, onError, delay: () => 20 });
    links.push(device.link);
    device.link.start();
    await server.until(() => device.link.status === "connected");
    return device.link;
  }

  const results = (id: string) => server.received.filter((f) => f.type === "op_result" && f.id === id);

  it("binds a chat when its operation arrives, tells the page once acknowledged, and runs the chat's work", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    await connect(chooser);
    const bind = bindOp(ROOT, ready);
    server.send(frame(bind));
    await server.until(() => results(bind.id).length === 1);
    expect(results(bind.id)[0]?.outcome).toEqual({ ok: null });
    const waiting = chooser.bindSession(ROOT, ready.token, WINDOW);
    server.send({ type: "op_ack", id: bind.id });
    expect(await waiting).toMatchObject({ root: ROOT, folder: notes });
    const work = { ...bind, id: "work", invocationId: "1:c", ordinal: 1, kind: "resolve", args: { path: "" }, digest: "w" };
    server.send(frame(work));
    await server.until(() => results("work").length === 1);
    expect(hosts.ran.map((operation) => operation.id)).toEqual(["work"]);
  });

  it("answers a bind at the next launch, without asking again, when the app stopped after recording it", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    const failures: unknown[] = [];
    const link = await connect(chooser, (error) => failures.push(error));
    // The app stops between recording the binding and recording its answer.
    vi.spyOn(journal, "answer").mockImplementation(() => {
      throw new Error("power lost");
    });
    const bind = bindOp(ROOT, ready);
    server.send(frame(bind));
    await server.until(() => failures.length === 1);
    await link.stop();
    journal.close();
    journals = journals.filter((opened) => opened !== journal);
    journal = open();
    expect(journal.openIds()).toEqual([bind.id]);
    await connect(binder(new User()));
    server.send(frame(bind));
    await server.until(() => results(bind.id).length === 1);
    expect(results(bind.id)[0]?.outcome).toEqual({ ok: null });
    expect(user.sheets).toHaveLength(1);
  });
});
