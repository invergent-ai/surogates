import { linkSync, mkdirSync, mkdtempSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

function binder(user: User, overrides: Partial<BinderOptions> = {}): Binder {
  return new Binder({
    bindings: journal.bindings,
    prompts: user,
    guards: { home: join(base, "home"), dataDir: join(base, "data"), appDirs: [join(base, "app")] },
    agent: "Research assistant",
    hosts,
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
  it("opens the dialog first when nothing was bound yet, then the sheet, and keeps the mode chosen there", async () => {
    const user = new User([notes], [{ mode: "ask" }]);
    const ready = await binder(user).prepareFolder("last", WINDOW, never());
    expect(user.dialogs).toEqual([join(base, "home")]);
    expect(user.sheets).toEqual([{ agent: "Research assistant", folder: notes, mode: "free", links: null, refusal: null }]);
    expect(ready).toEqual({
      folder: notes, mode: "ask", nonce: expect.stringMatching(/^[A-Za-z0-9_-]{16,128}$/), token: expect.any(String),
    });
    expect(ready?.token).not.toBe(ready?.nonce);
  });

  it("shows the last folder bound without a dialog, and opens the dialog there once that folder has gone", async () => {
    journal.bindings.add({ root: OTHER, nonce: "n".repeat(16), folder: notes, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    const user = new User([], [{ mode: "free" }]);
    expect((await binder(user).prepareFolder("last", WINDOW, never()))?.folder).toBe(notes);
    expect(user.dialogs).toEqual([]);
    rmSync(notes, { recursive: true });
    const again = new User([join(base, "other")], [{ mode: "free" }]);
    expect((await binder(again).prepareFolder("last", WINDOW, never()))?.folder).toBe(join(base, "other"));
    expect(again.dialogs).toEqual([notes]);
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
    expect(await chooser.admit(bindOp(ROOT, ready))).toEqual({ ok: null });
    const { dev, ino } = statSync(notes);
    expect(journal.bindings.get(ROOT)).toEqual({
      root: ROOT, nonce: ready.nonce, folder: notes, dev, ino, boot: BOOT_ID, mode: "ask", boundAt: expect.any(Number),
    });
  });

  it("is refused for a nonce this computer never gave out, or one already used, and binds nothing", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    expect(await chooser.admit(bindOp(ROOT, { ...ready, nonce: "x".repeat(32) }))).toEqual(NOT_BOUND);
    expect(await chooser.admit(bindOp(ROOT, ready))).toEqual({ ok: null });
    expect(await chooser.admit(bindOp(OTHER, ready))).toEqual(NOT_BOUND);
    expect(journal.bindings.get(OTHER)).toBeUndefined();
  });

  it("is refused, and uses up its confirmation, when it names another folder", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    expect(await chooser.admit(bindOp(ROOT, { ...ready, folder: `${notes}/` }))).toEqual(NOT_BOUND);
    expect(await chooser.admit(bindOp(ROOT, ready))).toEqual(NOT_BOUND);
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
    expect(await chooser.admit(bindOp(ROOT, ready, changes))).toEqual(NOT_BOUND);
    expect(journal.bindings.get(ROOT)).toBeUndefined();
  });

  it("is refused once its confirmation has expired, and the page is told so once", async () => {
    const user = new User();
    const chooser = binder(user, { preparedMs: 20 });
    const ready = await confirmed(user, chooser);
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(await chooser.admit(bindOp(ROOT, ready))).toEqual(NOT_BOUND);
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
    expect(await chooser.admit(bindOp(ROOT, ready))).toEqual({ ok: null });
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
    expect(await chooser.admit(bindOp(ROOT, ready))).toEqual(NOT_RECORDED);
    await expect(chooser.bindSession(ROOT, ready.token, WINDOW)).rejects.toBe(full);
    expect(failures).toEqual([full]);
    expect(journal.bindings.get(ROOT)).toBeUndefined();
  });

  it("is answered from the binding when it comes again after a restart, and refused once the chat is bound otherwise", async () => {
    const nonce = "n".repeat(32);
    journal.bindings.add({ root: ROOT, nonce, folder: notes, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    // A restarted app has no confirmations; the binding is in the journal.
    const restarted = binder(new User());
    expect(await restarted.admit(bindOp(ROOT, { folder: notes, nonce }))).toEqual({ ok: null });
    expect(await restarted.admit(bindOp(ROOT, { folder: notes, nonce: "y".repeat(32) }))).toEqual(ALREADY_BOUND);
    expect(await restarted.admit(bindOp(ROOT, { folder: join(base, "other"), nonce }))).toEqual(ALREADY_BOUND);
  });

  it("never answers with a message that ends in a full stop", () => {
    for (const outcome of [NOT_BOUND, ALREADY_BOUND, NOT_RECORDED]) {
      expect("error" in outcome && outcome.error.message.endsWith(".")).toBe(false);
    }
  });

  it("is the binder's own: every other operation goes to the tool hosts, and so does the end of access", async () => {
    const chooser = binder(new User());
    const op = { ...bindOp(ROOT, { folder: notes, nonce: "n" }), kind: "resolve", invocationId: "1:c", ordinal: 1 };
    expect(await chooser.admit(op)).toBeNull();
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
    await chooser.admit(bindOp(ROOT, ready));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(bound).toBeNull();
    chooser.acknowledged(`bind-${ROOT}`);
    await vi.waitFor(() => expect(bound).toMatchObject({ root: ROOT, folder: notes }));
    expect(await chooser.bindSession(ROOT, ready.token, WINDOW)).toEqual(bound);
    const later = await confirmed(user, chooser, join(base, "other"));
    await chooser.admit(bindOp(OTHER, later));
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
    await chooser.admit(bindOp(ROOT, ready));
    chooser.acknowledged(`bind-${ROOT}`);
    chooser.acknowledged(`bind-${ROOT}`);
    await vi.waitFor(() => expect(bound).toMatchObject({ root: ROOT, folder: notes }));
  });

  it("is refused for another window, an unknown token, and another chat", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    await chooser.admit(bindOp(ROOT, ready));
    chooser.acknowledged(`bind-${ROOT}`);
    await expect(chooser.bindSession(ROOT, ready.token, "window-2")).rejects.toThrow("not confirmed in this window");
    await expect(chooser.bindSession(ROOT, "unknown", WINDOW)).rejects.toThrow("not confirmed in this window");
    await expect(chooser.bindSession(OTHER, ready.token, WINDOW)).rejects.toThrow("This folder was confirmed for another chat");
  });

  it("is refused once this computer's access ended before the server recorded the bind", async () => {
    const user = new User();
    const chooser = binder(user);
    const ready = await confirmed(user, chooser);
    await chooser.admit(bindOp(ROOT, ready));
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
