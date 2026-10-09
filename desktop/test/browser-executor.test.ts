import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PAUSED } from "../src/browser/client.js";
import { interrupted, LEFT_TO_USER, type StagedDownload, UNSAVED } from "../src/browser/downloads.js";
import { Browsing, NO_BROWSER } from "../src/browser/executor.js";
import type { Launch } from "../src/browser/client.js";
import { MAX_READ_BYTES, MAX_WRITE_BYTES } from "../src/files/answers.js";
import { perform } from "../src/files/operations.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import type { ToolLayer } from "../src/shell/device-stack.js";

const ROOT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";
// Another chat of the agent's on this computer: its browser is the same one.
const OTHER = "5f6a7b8c-9d0e-4f1a-b2c3-d4e5f6a7b8c9";
const op = (kind: string, root = ROOT): Operation => ({
  id: `op-${kind}`, sessionId: root, callingSessionId: root, invocationId: "call", ordinal: 1, kind, args: {}, digest: "d",
});
const LAUNCH: Launch = { executable: "/opt/google/chrome/chrome", profile: "/data/browser-profiles/x/chrome" };
const signal = new AbortController().signal;

// Where the browser host stages downloads, its own temporary folder; and a folder of the user's beside it.
let staging: string;
let outside: string;
beforeEach(() => {
  const base = realpathSync(mkdtempSync(join(tmpdir(), "browsing-")));
  [staging, outside] = [join(base, "tmp"), join(base, "outside")];
  mkdirSync(staging);
  mkdirSync(outside);
});
afterEach(() => rmSync(dirname(staging), { recursive: true, force: true }));
// A file the host staged there, by its path.
const kept = (name: string): string => {
  writeFileSync(join(staging, name), "report");
  return join(staging, name);
};

// *reads*: what reads a chat's files in place of the rig's own, as the file helper would.
function rig(launch: Launch | null = LAUNCH, bound = true, reads?: (operation: Operation, signal: AbortSignal) => Promise<Outcome>) {
  const ran: string[] = [];
  const browsed: Array<{ launch: Launch; kind: string; args?: Record<string, unknown> }> = [];
  const stopped: string[] = [];
  const forgotten: string[] = [];
  const paused: Array<[string, boolean]> = [];
  // The uploads the browser was told are not coming, by their operations.
  const unasked: string[] = [];
  const shown: string[] = [];
  // What the browser's next answer is, and how it tells of a download it staged.
  const answers: Outcome[] = [];
  let staged: (download: StagedDownload) => void = () => {};
  // The chats this computer bound, until one is deleted.
  const chats = new Set(bound ? [ROOT, OTHER] : []);
  // The chat's files, as its file host reads them.
  const files: Record<string, string> = { "/home/u/notes/report.pdf": "%PDF-1.7", "/home/u/notes/scan.png": "PNG" };
  // What happens while a file is read, as the test says: nothing unless it does.
  const reading = { then: (): void => {} };
  // Each question the sandbox was asked: whether a chat listens on a port. It does on 3000 alone.
  const probed: Array<[string, number]> = [];
  const tools: ToolLayer = {
    run: (operation, stop) => {
      ran.push(operation.kind);
      if (operation.kind !== "read" || operation.args.key === undefined) return Promise.resolve({ ok: "tools" });
      if (reads) return reads(operation, stop);
      reading.then();
      const data = files[String(operation.args.key)];
      return Promise.resolve(data === undefined
        ? { error: { type: "os", code: "ENOENT", message: "No such file or directory", filename: String(operation.args.key) } }
        : { ok: Buffer.from(data).toString("base64") });
    },
    refusal: () => ({ error: { type: "other", message: "from the tools" } }),
    guards: () => ({ home: "/home/u", dataDir: "/data", cacheDir: "/home/u/.cache/surogate", appDirs: [] }),
    live: () => [],
    stop: () => (stopped.push("tools"), Promise.resolve()),
    end: () => (stopped.push("tools ended"), Promise.resolve()),
    retired: (root) => void forgotten.push(`tools ${root}`),
  };
  const browsing = new Browsing({
    tools,
    browser: {
      perform: (chosen, operation) => (
        browsed.push({ launch: chosen, kind: operation.kind, ...(operation.kind === "browser.set_input_files" ? { args: operation.args } : {}) }),
        Promise.resolve<Outcome>(answers.shift() ?? { ok: "browser" })
      ),
      forget: (root) => void forgotten.push(`browser ${root}`),
      stop: () => (stopped.push("browser"), Promise.resolve()),
      end: () => (stopped.push("browser ended"), Promise.resolve()),
      address: (session, upload, of, root) => Promise.resolve(
        session === "nowhere"
          ? { refused: "no site" }
          : `https://example.com/${session}${upload ? "/the-input" : ""}${of === undefined ? "" : `#${of}`}${root === undefined ? "" : `@${root}`}`,
      ),
      pause: (root, held) => void paused.push([root, held]),
      notComing: (of) => void unasked.push(of),
      show: (root) => (shown.push(root), Promise.resolve(true)),
      onDownload: (listener) => {
        staged = listener;
      },
    },
    bindingOf: (root) => (chats.has(root) ? {} : undefined),
    launch: () => launch,
    staging,
    vm: { listening: (root, port) => (probed.push([root, port]), Promise.resolve(port === 3000)) },
  });
  return { browsing, ran, browsed, stopped, forgotten, paused, shown, chats, answers, reading, files, unasked, probed, stage: (download: StagedDownload) => staged(download) };
}

describe("the browser's kinds beside the tools", () => {
  it("sends the browser's kinds to the browser with the launch chosen now, and every other kind to the tools", async () => {
    const { browsing, ran, browsed } = rig();
    expect(await browsing.run(op("browser.navigate"), signal)).toEqual({ ok: "browser" });
    expect(await browsing.run(op("read"), signal)).toEqual({ ok: "tools" });
    expect(browsed).toEqual([{ launch: LAUNCH, kind: "browser.navigate" }]);
    expect(ran).toEqual(["read"]);
  });

  it("refuses the browser's kinds before anyone is asked when no browser is here, and leaves the tools' own refusals to them", async () => {
    expect(rig(null).browsing.refusal(op("browser.observe"))).toEqual(NO_BROWSER);
    expect(rig().browsing.refusal(op("browser.observe"))).toBeNull();
    expect(rig().browsing.refusal(op("write"))).toEqual({ error: { type: "other", message: "from the tools" } });
    expect(await rig(null).browsing.run(op("browser.navigate"), signal)).toEqual(NO_BROWSER);
  });

  it("runs no browser operation for a chat this computer did not bind", async () => {
    const { browsing, browsed } = rig(LAUNCH, false);
    expect(await browsing.run(op("browser.navigate"), signal)).toEqual(FOLDER_UNAVAILABLE);
    expect(browsed).toEqual([]);
  });

  it("answers every chat's browser operations paused_by_user while a chat's user holds the browser, before anyone is asked, until that chat hands it back", async () => {
    const { browsing, browsed, paused, ran } = rig();
    expect(browsing.takeOver(ROOT)).toBe(true);
    expect(browsing.takenOver(ROOT)).toBe(true);
    // The browser is the agent's one browser here: a chat that never asked for the take-over is answered so too.
    for (const root of [ROOT, OTHER]) {
      for (const kind of ["browser.navigate", "browser.observe", "browser.close"]) {
        expect(browsing.refusal(op(kind, root))).toEqual(PAUSED);
        // One the binder let through before the take-over is answered so too, and never reaches the browser.
        expect(await browsing.run(op(kind, root), signal)).toEqual(PAUSED);
      }
    }
    expect(browsed).toEqual([]);
    // A chat's other tools are not the browser's: they run as before.
    expect(browsing.refusal(op("read"))).toEqual({ error: { type: "other", message: "from the tools" } });
    expect(await browsing.run(op("read"), signal)).toEqual({ ok: "tools" });
    expect(ran).toEqual(["read"]);
    // Handed back, and said so: then there is nothing more to hand back, which is said too.
    expect(browsing.handBack(ROOT)).toBe(true);
    expect(browsing.handBack(ROOT)).toBe(false);
    expect(browsing.takenOver(ROOT)).toBe(false);
    expect(browsing.takenOver(OTHER)).toBe(false);
    for (const root of [ROOT, OTHER]) {
      expect(browsing.refusal(op("browser.navigate", root))).toBeNull();
      expect(await browsing.run(op("browser.navigate", root), signal)).toEqual({ ok: "browser" });
    }
    // The browser host is told each, for an operation already waiting or acting there.
    expect(paused).toEqual([[ROOT, true], [ROOT, false]]);
  });

  it("lets no other chat take the browser from the chat that holds it, nor hand it back", async () => {
    const { browsing, paused } = rig();
    expect(browsing.takeOver(ROOT)).toBe(true);
    // Another chat's take-over does not steal it: that chat is told it is held elsewhere, and the first still holds it.
    expect(browsing.takeOver(OTHER)).toBe(false);
    expect(browsing.takenOver(OTHER)).toBe("elsewhere");
    expect(browsing.takenOver(ROOT)).toBe(true);
    // Nor does its hand back end it, and it is told that nothing was handed back.
    expect(browsing.handBack(OTHER)).toBe(false);
    expect(browsing.takenOver(ROOT)).toBe(true);
    expect(browsing.refusal(op("browser.navigate", OTHER))).toEqual(PAUSED);
    // Taken over again by the chat that holds it: held as before, and the host is told nothing more.
    expect(browsing.takeOver(ROOT)).toBe(true);
    expect(paused).toEqual([[ROOT, true]]);
  });

  it("keeps the browser held when the chat that took it over is deleted, and lets any chat hand it back then", async () => {
    const { browsing, browsed, chats, paused } = rig();
    browsing.takeOver(ROOT);
    // Deleted, as a page can have a chat deleted: nothing is handed back by that.
    browsing.retired(ROOT);
    chats.delete(ROOT);
    expect(browsing.refusal(op("browser.navigate", OTHER))).toEqual(PAUSED);
    expect(await browsing.run(op("browser.navigate", OTHER), signal)).toEqual(PAUSED);
    expect(browsed).toEqual([]);
    // No chat holds it now, and every chat is told so: neither "held from this chat" nor "not held".
    expect(browsing.takenOver(OTHER)).toBe("orphaned");
    // The chat that held it can hand nothing back, so any chat's hand back ends it: the desktop confirms that one as any.
    expect(browsing.handBack(OTHER)).toBe(true);
    expect(browsing.takenOver(OTHER)).toBe(false);
    expect(browsing.refusal(op("browser.navigate", OTHER))).toBeNull();
    expect(paused).toEqual([[ROOT, true], [ROOT, false]]);
  });

  it("hands the browser back for no chat, as the desktop's own Settings asks, only where it is held from a chat that is gone: held from one that is here it is that chat's to hand back, and held by nobody nothing is", () => {
    const { browsing, chats, paused } = rig();
    expect([browsing.heldFromGone(), browsing.handBackGone()]).toEqual([false, false]);
    browsing.takeOver(ROOT);
    expect([browsing.heldFromGone(), browsing.handBackGone(), browsing.takenOver(ROOT)]).toEqual([false, false, true]);
    // The chat is deleted: no page of its is left to hand the browser back in.
    browsing.retired(ROOT);
    chats.delete(ROOT);
    expect([browsing.heldFromGone(), browsing.refusal(op("browser.navigate", OTHER))]).toEqual([true, PAUSED]);
    expect(browsing.handBackGone()).toBe(true);
    // Handed back: the browser host is told for the chat that held it, every chat's operations run, and nothing is left to hand back.
    expect([paused, browsing.takenOver(OTHER), browsing.refusal(op("browser.navigate", OTHER))]).toEqual([[[ROOT, true], [ROOT, false]], false, null]);
    expect([browsing.heldFromGone(), browsing.handBackGone()]).toEqual([false, false]);
  });

  it("takes a deleted chat for gone though its folder could not be forgotten here, and lets the next chat take the browser over as well", () => {
    const { browsing, paused } = rig();
    browsing.takeOver(ROOT);
    // Deleted, its binding left behind.
    browsing.retired(ROOT);
    expect(browsing.takenOver(OTHER)).toBe("orphaned");
    // Taken over from another chat: that one holds it now, as any holder.
    expect(browsing.takeOver(OTHER)).toBe(true);
    expect(browsing.takenOver(OTHER)).toBe(true);
    expect(browsing.takenOver(ROOT)).toBe("elsewhere");
    // A hand back confirmed for the deleted chat's sake, while it was gone, releases nothing now: another chat holds it.
    expect(browsing.handBack(ROOT)).toBe(false);
    expect(browsing.takenOver(OTHER)).toBe(true);
    expect(browsing.handBack(OTHER)).toBe(true);
    expect(browsing.takenOver(OTHER)).toBe(false);
    expect(paused).toEqual([[ROOT, true], [OTHER, true], [OTHER, false]]);
    // Held anew from a chat that is here, it is that chat's alone again.
    browsing.takeOver(OTHER);
    expect(browsing.takenOver(ROOT)).toBe("elsewhere");
  });

  it("tells each chat where the agent's browser is held: from it, by nobody, from another chat that is here, or from one that is gone", () => {
    const { browsing, chats } = rig();
    const told = () => [browsing.takenOver(ROOT), browsing.takenOver(OTHER)];
    expect(told()).toEqual([false, false]);
    browsing.takeOver(ROOT);
    // The other chat can neither take it nor hand it back, and is told so: not "nobody holds it".
    expect(told()).toEqual([true, "elsewhere"]);
    browsing.handBack(ROOT);
    expect(told()).toEqual([false, false]);
    browsing.takeOver(OTHER);
    expect(told()).toEqual(["elsewhere", true]);
    // The chat it is held from is gone: any chat may hand it back, and each that is left is told that.
    chats.delete(OTHER);
    expect(browsing.takenOver(ROOT)).toBe("orphaned");
    browsing.handBack(ROOT);
    expect(browsing.takenOver(ROOT)).toBe(false);
  });

  it("takes a chat whose folder was forgotten on this computer for gone too", () => {
    const { browsing, chats } = rig();
    browsing.takeOver(ROOT);
    chats.delete(ROOT);
    expect(browsing.takenOver(OTHER)).toBe("orphaned");
    browsing.handBack(OTHER);
    expect(browsing.takenOver(OTHER)).toBe(false);
  });

  it("asks the browser to show a chat's page", async () => {
    const { browsing, shown } = rig();
    expect(await browsing.show(ROOT)).toBe(true);
    expect(shown).toEqual([ROOT]);
  });

  it("saves each download the browser staged with what the stack saves it by, and tells its session at its next answer that says what its page did", async () => {
    const { browsing, answers, stage } = rig();
    const saved: StagedDownload[] = [];
    browsing.saveDownloadsWith((download) => (saved.push(download), Promise.resolve(`saved ${download.name}`)));
    const download = { root: ROOT, session: "child", name: "report.txt", path: kept("a"), user: false };
    // One its user started while they held the browser: saved as theirs, and nothing of it is the agent's to hear.
    const own = { ...download, name: "statement.pdf", path: kept("b"), user: true };
    stage(download);
    stage(own);
    await vi.waitFor(() => expect(saved).toEqual([download, own]));
    const of = (kind: string, calling: string): Operation => ({ ...op(kind), callingSessionId: calling });
    // A read says nothing of what the page did; another session's answer is not this one's.
    answers.push({ ok: { frames: [] } }, { ok: { notices: [] } }, { ok: { url: "u", title: "t", opened: false, notices: ["The page asked for a file to upload."] } });
    expect(await browsing.run(of("browser.observe", "child"), signal)).toEqual({ ok: { frames: [] } });
    expect(await browsing.run(of("browser.mouse", ROOT), signal)).toEqual({ ok: { notices: [] } });
    expect(await browsing.run(of("browser.navigate", "child"), signal)).toEqual({
      ok: { url: "u", title: "t", opened: false, notices: ["The page asked for a file to upload.", "saved report.txt"] },
    });
    // Told once.
    answers.push({ ok: { notices: [] } });
    expect(await browsing.run(of("browser.keyboard", "child"), signal)).toEqual({ ok: { notices: [] } });
  });

  it("keeps what a download came to through a take-over: nothing of it rides on an answer that is paused, and its session hears once the browser is its agent's again", async () => {
    const { browsing, browsed, answers, stage } = rig();
    browsing.saveDownloadsWith((download) => Promise.resolve(`saved ${download.name}`));
    const told = (browsing as unknown as { told: Map<string, { notices: string[] }> }).told;
    // Staged before its user took the browser over: the agent's, saved as usual while they hold it.
    stage({ root: ROOT, session: ROOT, name: "report.txt", path: kept("a"), user: false });
    await vi.waitFor(() => expect(told.get(ROOT)?.notices).toEqual(["saved report.txt"]));
    browsing.takeOver(OTHER);
    expect(await browsing.run(op("browser.navigate"), signal)).toEqual(PAUSED);
    expect(browsed).toEqual([]);
    browsing.handBack(OTHER);
    // One the browser itself answers paused, as it answers what was acting when it was taken over.
    answers.push(PAUSED, { ok: { notices: [] } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual(PAUSED);
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: ["saved report.txt"] } });
    expect(told.size).toBe(0);
  });

  it("keeps what a download came to for the session's next answer where the one that would have carried it was made for an operation already cancelled: that answer reaches no one", async () => {
    const { browsing, answers, stage } = rig();
    browsing.saveDownloadsWith((download) => Promise.resolve(`saved ${download.name}`));
    const told = (browsing as unknown as { told: Map<string, { notices: string[] }> }).told;
    stage({ root: ROOT, session: ROOT, name: "report.txt", path: kept("a"), user: false });
    await vi.waitFor(() => expect(told.get(ROOT)?.notices).toEqual(["saved report.txt"]));
    // Cancelled while the browser acted, which answers as if it had not been.
    const cancelled = new AbortController();
    cancelled.abort();
    answers.push({ ok: { notices: [] } }, { ok: { notices: [] } });
    expect(await browsing.run(op("browser.mouse"), cancelled.signal)).toEqual({ ok: { notices: [] } });
    expect(told.get(ROOT)?.notices).toEqual(["saved report.txt"]);
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: ["saved report.txt"] } });
  });

  it("goes on to the next download after one that what saves it failed on, and tells nothing of that one", async () => {
    const { browsing, answers, stage } = rig();
    let saves = 0;
    browsing.saveDownloadsWith((download) => ((saves += 1) === 1 ? Promise.reject(new Error("the journal is closed")) : Promise.resolve(`saved ${download.name}`)));
    const told = (browsing as unknown as { told: Map<string, unknown> }).told;
    const first = kept("a");
    stage({ root: ROOT, session: ROOT, name: "first.txt", path: first, user: false });
    stage({ root: ROOT, session: ROOT, name: "second.txt", path: kept("b"), user: false });
    await vi.waitFor(() => expect(told.size).toBe(1));
    // What it failed on is not left staged.
    await vi.waitFor(() => expect(existsSync(first)).toBe(false));
    answers.push({ ok: { notices: [] } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: ["saved second.txt"] } });
  });

  it("tells a session what came of a download taken for its user's only because it came just after they handed the browser back, and never of one that is theirs outright, whatever came of it", async () => {
    const { browsing, answers, stage } = rig();
    const saved: string[] = [];
    browsing.saveDownloadsWith((download) => (saved.push(download.name), Promise.resolve(`saved ${download.name}`)));
    const theirs = { root: ROOT, session: ROOT, user: true };
    // Theirs outright: saved, or no file this side takes for staged. Not a word of either.
    stage({ ...theirs, name: "statement.pdf", path: kept("a") });
    stage({ ...theirs, name: "payslip.pdf", path: join(outside, "nothing") });
    // Theirs only by the minute, and so perhaps the agent's own: what the saver says came of it; and of one this
    // side takes for no staged file, that it was not saved, and no more.
    stage({ ...theirs, name: "report.txt", path: kept("b"), afterHandBack: true });
    stage({ ...theirs, name: "lost.txt", path: join(outside, "nothing"), afterHandBack: true });
    const told = (browsing as unknown as { told: Map<string, { notices: string[] }> }).told;
    await vi.waitFor(() => expect(told.get(ROOT)?.notices).toHaveLength(2));
    expect(saved).toEqual(["statement.pdf", "report.txt"]);
    answers.push({ ok: { notices: [] } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: ["saved report.txt", LEFT_TO_USER] } });
    // What saves it failing outright is "not saved" too; of one that is theirs outright, still not a word.
    browsing.saveDownloadsWith(() => Promise.reject(new Error("the journal is closed")));
    stage({ ...theirs, name: "statement.pdf", path: kept("c") });
    stage({ ...theirs, name: "report.txt", path: kept("d"), afterHandBack: true });
    await vi.waitFor(() => expect(told.get(ROOT)?.notices).toEqual([LEFT_TO_USER]));
    await vi.waitFor(() => expect([existsSync(join(staging, "c")), existsSync(join(staging, "d"))]).toEqual([false, false]));
    answers.push({ ok: { notices: [] } }, { ok: { notices: [] } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: [LEFT_TO_USER] } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: [] } });
    // The agent's own is told as its own, though it came marked so by mistake.
    stage({ root: ROOT, session: ROOT, name: "mine.txt", path: join(outside, "nothing"), user: false, afterHandBack: true });
    await vi.waitFor(() => expect(told.get(ROOT)?.notices).toEqual([UNSAVED]));
  });

  it("drops a download the browser hands on as the agent's once its user has taken the browser over, which this side knows before the browser does; one of their own is saved", async () => {
    const { browsing, answers, stage } = rig();
    const saved: string[] = [];
    browsing.saveDownloadsWith((download) => (saved.push(download.name), Promise.resolve(`saved ${download.name}`)));
    browsing.takeOver(OTHER);
    // Handed on by a browser host that had not heard of the take-over yet.
    const agents = kept("a");
    stage({ root: ROOT, session: ROOT, name: "report.txt", path: agents, user: false });
    stage({ root: ROOT, session: ROOT, name: "statement.pdf", path: kept("b"), user: true });
    await vi.waitFor(() => expect([saved, existsSync(agents)]).toEqual([["statement.pdf"], false]));
    // Handed back, its agent hears what became of its own, as of one the browser had still held; and nothing of its user's.
    browsing.handBack(OTHER);
    answers.push({ ok: { notices: [] } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: [interrupted("report.txt")] } });
    expect(interrupted("report.txt")).toBe(
      'The page\'s download of "report.txt" was interrupted when the user took over the agent\'s browser on this computer, so it was not saved.',
    );
    // One handed on once the browser is the agent's again is saved as its own: also where it is taken over
    // in the next instant, before this side has looked at where the file is.
    stage({ root: ROOT, session: ROOT, name: "report.txt", path: kept("c"), user: false });
    browsing.takeOver(OTHER);
    await vi.waitFor(() => expect(saved).toEqual(["statement.pdf", "report.txt"]));
  });

  it("tells a session of twenty downloads at most with one answer, and removes a staged file nothing was given to save with", async () => {
    const { browsing, answers, stage } = rig();
    // Before the stack has said what saves them: the staged file goes, and nobody is told.
    const unsaved = kept("staged");
    stage({ root: ROOT, session: ROOT, name: "report.txt", path: unsaved, user: false });
    await vi.waitFor(() => expect(existsSync(unsaved)).toBe(false));
    let saved = 0;
    browsing.saveDownloadsWith((download) => (saved += 1, Promise.resolve(`saved ${download.name}`)));
    // Staged at once, every other one behind a chain of links, which takes longer to follow: saved in the order they came all the same.
    const paths = Array.from({ length: 21 }, (_, at) => {
      let path = kept(String(at + 1));
      for (let link = 0; at % 2 === 0 && link < 30; link += 1) {
        symlinkSync(path, join(staging, `${at + 1}-link-${link}`));
        path = join(staging, `${at + 1}-link-${link}`);
      }
      return path;
    });
    paths.forEach((path, at) => stage({ root: ROOT, session: ROOT, name: `${at + 1}.txt`, path, user: false }));
    await vi.waitFor(() => expect(saved).toBe(21));
    answers.push({ ok: { notices: [] } }, { ok: { notices: [] } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: Array.from({ length: 20 }, (_, at) => `saved ${at + 1}.txt`) } });
    expect(await browsing.run(op("browser.mouse"), signal)).toEqual({ ok: { notices: [] } });
  });

  it("reads and removes a staged file only under the folder its browser host stages in: any other is left as it is, given to nothing that saves, and said not to be saved", async () => {
    const { browsing, answers, stage } = rig();
    const elsewhere = join(outside, "id_rsa");
    writeFileSync(elsewhere, "a file of the user's");
    symlinkSync(elsewhere, join(staging, "link"));
    symlinkSync(outside, join(staging, "folder"));
    // A file elsewhere, by its path, by a link in the folder, through a linked folder and by a path that climbs out;
    // nothing; the folder itself; and no path at all.
    const others = [
      elsewhere, join(staging, "link"), join(staging, "folder", "id_rsa"), `${staging}/../outside/id_rsa`, join(staging, "gone"), staging,
      undefined as unknown as string,
    ];
    const staged = (path: string, user = false): StagedDownload => ({ root: ROOT, session: ROOT, name: "report.txt", path, user });
    const told = (browsing as unknown as { told: Map<string, { notices: string[] }> }).told;
    // With nothing to save with yet, a staged file is removed: none of these is one.
    for (const path of others) stage(staged(path));
    await vi.waitFor(() => expect(told.get(ROOT)?.notices).toHaveLength(others.length));
    expect([readFileSync(elsewhere, "utf8"), existsSync(join(staging, "link")), existsSync(staging)]).toEqual(["a file of the user's", true, true]);
    const saved: StagedDownload[] = [];
    browsing.saveDownloadsWith((download) => (saved.push(download), Promise.resolve(`saved ${download.name}`)));
    for (const path of others) stage(staged(path));
    // Of one that was its user's, nothing is told either way.
    stage(staged(elsewhere, true));
    // One that is there is handed on by its real path, a link in the folder that leads within it followed.
    mkdirSync(join(staging, "playwright-artifacts-x"));
    symlinkSync(join(staging, "playwright-artifacts-x"), join(staging, "artifacts"));
    stage(staged(join(staging, "artifacts", kept("playwright-artifacts-x/guid").slice(-4))));
    await vi.waitFor(() => expect(saved).toEqual([staged(join(staging, "playwright-artifacts-x", "guid"))]));
    await vi.waitFor(() => expect(told.get(ROOT)?.notices).toHaveLength(2 * others.length + 1));
    answers.push({ ok: { notices: [] } });
    const heard = (await browsing.run(op("browser.mouse"), signal) as { ok: { notices: string[] } }).ok.notices;
    expect([heard.filter((notice) => notice === UNSAVED).length, heard.filter((notice) => notice !== UNSAVED)]).toEqual([2 * others.length, ["saved report.txt"]]);
    expect(UNSAVED).toBe("The page downloaded a file, but it was not saved: this computer could not save it.");
    expect(readFileSync(elsewhere, "utf8")).toBe("a file of the user's");
  });

  it("tells what saves a deleted chat's downloads to stop, its sub-agents' too, and no other chat's", async () => {
    const { browsing, stage } = rig();
    const stops = new Map<string, AbortSignal>();
    browsing.saveDownloadsWith((download, stop) => (stops.set(download.name, stop), new Promise(() => {})));
    stage({ root: ROOT, session: ROOT, name: "report.txt", path: kept("a"), user: false });
    stage({ root: ROOT, session: "child", name: "statement.pdf", path: kept("b"), user: true });
    stage({ root: OTHER, session: OTHER, name: "other.txt", path: kept("c"), user: false });
    await vi.waitFor(() => expect(stops.size).toBe(3));
    expect([...stops.values()].map((stop) => stop.aborted)).toEqual([false, false, false]);
    browsing.retired(ROOT);
    expect([...stops].map(([name, stop]) => [name, stop.aborted])).toEqual([["report.txt", true], ["statement.pdf", true], ["other.txt", false]]);
    // Nothing is kept of a chat that is gone; one of its downloads that comes after is told to stop by nothing old.
    expect((browsing as unknown as { saving: Map<string, unknown> }).saving.has(ROOT)).toBe(false);
    stage({ root: ROOT, session: ROOT, name: "late.txt", path: kept("d"), user: false });
    await vi.waitFor(() => expect(stops.get("late.txt")?.aborted).toBe(false));
  });

  it("drops what a deleted chat's downloads came to, untold: its own and its sub-agents', and of a save that ends after it", async () => {
    const { browsing, chats, stage } = rig();
    let ended = (): void => {};
    browsing.saveDownloadsWith((download) => (download.name === "late.txt"
      ? new Promise((resolve) => {
        ended = () => resolve("saved late.txt");
      })
      : Promise.resolve(`saved ${download.name}`)));
    const told = (browsing as unknown as { told: Map<string, unknown> }).told;
    stage({ root: ROOT, session: ROOT, name: "report.txt", path: kept("a"), user: false });
    stage({ root: ROOT, session: "child", name: "notes.txt", path: kept("b"), user: false });
    stage({ root: OTHER, session: OTHER, name: "other.txt", path: kept("c"), user: false });
    stage({ root: ROOT, session: "child", name: "late.txt", path: kept("d"), user: false });
    await vi.waitFor(() => expect([...told.keys()]).toEqual([ROOT, "child", OTHER]));
    // Deleted, as the server retires a chat: its sessions get no answer more to carry them.
    browsing.retired(ROOT);
    chats.delete(ROOT);
    expect([...told.keys()]).toEqual([OTHER]);
    // One of its saves that ends only now is kept for nobody.
    ended();
    await new Promise((done) => setTimeout(done, 50));
    expect([...told.keys()]).toEqual([OTHER]);
  });

  it("reads each file of an upload through the chat's file host, and gives the browser what they hold, never their paths", async () => {
    const { browsing, browsed, ran } = rig();
    const upload = { ...op("browser.set_input_files"), args: { paths: ["/home/u/notes/report.pdf", "/home/u/notes/scan.png"] } };
    expect(await browsing.run(upload, signal)).toEqual({ ok: "browser" });
    expect(ran).toEqual(["read", "read"]);
    expect(browsed.at(-1)?.args).toEqual({ files: [
      { name: "report.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7").toString("base64") },
      { name: "scan.png", mimeType: "image/png", buffer: Buffer.from("PNG").toString("base64") },
    ] });
    // One the file host refuses, as one gone or outside the folder: answered so, and the browser is given nothing.
    const missing = { ...upload, args: { paths: ["/home/u/notes/report.pdf", "/home/u/notes/gone.txt"] } };
    expect(await browsing.run(missing, signal)).toEqual({
      error: { type: "browser", message: "/home/u/notes/gone.txt could not be read for the page: No such file or directory" },
    });
    expect(browsed).toHaveLength(1);
    expect(await browsing.run({ ...upload, args: { paths: "report.pdf" } }, signal)).toMatchObject({ error: { type: "browser" } });
  });

  it("gives the browser none of an upload's files once its user took the browser over while they were read, and reads no more of them", async () => {
    const { browsing, browsed, ran, reading } = rig();
    const upload = { ...op("browser.set_input_files"), args: { paths: ["/home/u/notes/report.pdf", "/home/u/notes/scan.png"] } };
    // Taken over from another chat while the first file is read: the browser is the agent's one browser here.
    reading.then = () => void browsing.takeOver(OTHER);
    expect(await browsing.run(upload, signal)).toEqual(PAUSED);
    expect(ran).toEqual(["read"]);
    expect(browsed).toEqual([]);
    // Handed back, it is not taken up again: only an upload sent anew gives its files.
    reading.then = () => {};
    browsing.handBack(OTHER);
    expect(browsed).toEqual([]);
    expect(await browsing.run(upload, signal)).toEqual({ ok: "browser" });
    expect(browsed).toHaveLength(1);
    // Taken over as the last file was read: none of them leaves this process.
    reading.then = () => void (ran.filter((kind) => kind === "read").length === 5 && browsing.takeOver(ROOT));
    expect(await browsing.run(upload, signal)).toEqual(PAUSED);
    expect(ran.filter((kind) => kind === "read")).toHaveLength(5);
    expect(browsed).toHaveLength(1);
    browsing.handBack(ROOT);
    // Taken over and handed back while a file was read, the first or the last: its user has held the browser
    // since the upload began, so it is not taken up again, and no more of it is read.
    for (const [at, reads] of [[6, 6], [8, 8]] as const) {
      reading.then = () => {
        if (ran.filter((kind) => kind === "read").length !== at) return;
        browsing.takeOver(OTHER);
        browsing.handBack(OTHER);
      };
      expect(await browsing.run(upload, signal), `read ${at}`).toEqual(PAUSED);
      expect(ran.filter((kind) => kind === "read"), `read ${at}`).toHaveLength(reads);
      expect(browsed).toHaveLength(1);
    }
    // One begun once the browser is the agent's again gives its files.
    reading.then = () => {};
    expect(await browsing.run(upload, signal)).toEqual({ ok: "browser" });
    expect(browsed).toHaveLength(2);
  });

  it("gives the browser none of an upload that does not name one to ten files, and reads nothing for it", async () => {
    const { browsing, browsed, ran } = rig();
    const names = (paths: unknown) => browsing.run({ ...op("browser.set_input_files"), args: { paths } }, signal);
    const one = "/home/u/notes/report.pdf";
    for (const paths of [undefined, null, one, {}, [], Array.from({ length: 11 }, () => one), [one, 7], [one, ""], [one, null], [[one]]]) {
      expect(await names(paths), JSON.stringify(paths)).toEqual({ error: { type: "browser", message: "An upload names 1 to 10 files of the chat's folder" } });
    }
    expect([ran, browsed]).toEqual([[], []]);
    // Ten are read, each once, and given.
    expect(await names(Array.from({ length: 10 }, () => one))).toEqual({ ok: "browser" });
    expect(ran).toEqual(Array.from({ length: 10 }, () => "read"));
    expect((browsed[0]?.args as { files: unknown[] }).files).toHaveLength(10);
  });

  it("refuses an upload that names a file by a path that holds half a character, before anyone is asked: written out, it names another file than the one that would be read", async () => {
    const { browsing, browsed, ran } = rig();
    const names = (...paths: string[]) => ({ ...op("browser.set_input_files"), args: { paths } });
    const good = "/home/u/notes/report.pdf";
    for (const [bad, shown] of [["/home/u/notes/x\ud800.txt", "\\ud800"], ["/home/u/notes/x\udc00.txt", "\\udc00"], ["/home/u/no\udbfftes/report.pdf", "\\udbff"]] as const) {
      const refused = { error: { type: "browser", message: `An upload gives a page no file whose path holds half a character, which no file's name can: ${JSON.stringify(bad)}` } };
      expect(refused.error.message, bad).toContain(shown);
      for (const paths of [[bad], [good, bad]]) {
        expect(browsing.refusal(names(...paths)), JSON.stringify(paths)).toEqual(refused);
        expect(await browsing.run(names(...paths), signal), JSON.stringify(paths)).toEqual(refused);
      }
    }
    expect([ran, browsed]).toEqual([[], []]);
    // Both halves, in their order, are one character, and a name as any other.
    expect(browsing.refusal(names("/home/u/notes/\ud83d\ude00.pdf"))).toBeNull();
  });

  it("refuses an upload that names a file by a path with a control character, or a line or paragraph separator, before anyone is asked, and says why", async () => {
    const { browsing, browsed, ran } = rig();
    const names = (...paths: string[]) => ({ ...op("browser.set_input_files"), args: { paths } });
    const good = "/home/u/notes/report.pdf";
    for (const [bad, shown] of [
      ["/home/u/notes/public.txt\n/home/u/notes/draft.txt", "\\n"], ["/home/u/notes/a\rb.txt", "\\r"], ["/home/u/notes/a\tb.txt", "\\t"], ["/home/u/notes/a\u0000b", "\\u0000"],
      ["/home/u/notes/a\u007fb", "\u007f"], ["/home/u/notes/a\u0085b", "\u0085"], ["/home/u/notes/a\u2028b", "\u2028"], ["/home/u/notes/a\u2029b", "\u2029"],
      ["/home/u/no\ntes/report.pdf", "\\n"],
    ] as const) {
      const refused = { error: { type: "browser", message: `An upload gives a page no file whose path holds a line break or another control character: ${JSON.stringify(bad)}` } };
      expect(refused.error.message, bad).toContain(shown);
      for (const paths of [[bad], [good, bad]]) {
        // Before the chat's user is asked about it, and where nobody is, as in a chat that works freely.
        expect(browsing.refusal(names(...paths)), JSON.stringify(paths)).toEqual(refused);
        expect(await browsing.run(names(...paths), signal), JSON.stringify(paths)).toEqual(refused);
      }
    }
    expect([ran, browsed]).toEqual([[], []]);
    // A name with a space, a quote, a letter of another script or a mark that shows is a name as any other.
    expect(browsing.refusal(names("/home/u/notes/a b 'c' \"d\" é 日本 <e>.pdf"))).toBeNull();
    // A file's own name with a backslash in it, which a page can take for a folder's: refused the same way, before
    // anyone is asked and before anything is read. A folder on the way to the file may be named so.
    const slashed = "/home/u/notes/..\\secret.txt";
    const refused = { error: { type: "browser", message: `An upload gives a page no file whose name holds a backslash, which a page can take for a folder's: ${JSON.stringify(slashed)}` } };
    for (const paths of [[slashed], [good, slashed]]) {
      expect(browsing.refusal(names(...paths))).toEqual(refused);
      expect(await browsing.run(names(...paths), signal)).toEqual(refused);
    }
    expect([ran, browsed]).toEqual([[], []]);
    expect(browsing.refusal(names("/home/u/no\\tes/report.pdf"))).toBeNull();
    // While its user holds the browser an upload is answered paused, as every operation in it.
    browsing.takeOver(ROOT);
    expect(browsing.refusal(names("/home/u/notes/a\nb"))).toEqual(PAUSED);
  });

  it("gives the browser nothing where the chat's file host answers a read with anything but the file's data", async () => {
    for (const answered of [{ ok: null }, { ok: 7 }, { ok: { transfer: { size: 3, sha256: "x" } } }, { ok: ["UE5H"] }] as Outcome[]) {
      const { browsing, browsed } = rig(LAUNCH, true, () => Promise.resolve(answered));
      expect(await browsing.run({ ...op("browser.set_input_files"), args: { paths: ["/home/u/notes/scan.png"] } }, signal), JSON.stringify(answered)).toEqual({
        error: { type: "browser", message: "/home/u/notes/scan.png could not be read for the page: its file host answered no data" },
      });
      expect(browsed).toEqual([]);
    }
  });

  it("asks the sandbox whether a chat listens on a port of its own, and answers as it does", async () => {
    const { browsing, probed, browsed, ran } = rig();
    expect([await browsing.listening(ROOT, 3000), await browsing.listening(OTHER, 9)]).toEqual([true, false]);
    // The sandbox's own question: nothing of the browser's or the tools' is run for it.
    expect([probed, browsed, ran]).toEqual([[[ROOT, 3000], [OTHER, 9]], [], []]);
  });

  it("tells the browser of an upload that ends before it reaches it, whatever it ends on, and of one the approvals say got no leave: the browser keeps nothing for either", async () => {
    const upload = (id: string, paths: unknown = ["/home/u/notes/report.pdf"], root = ROOT) => ({ ...op("browser.set_input_files", root), id, args: { paths } });
    // The approvals' word is passed on as it is.
    const { browsing, unasked, reading, browsed } = rig();
    browsing.notComing("denied");
    expect(unasked.splice(0)).toEqual(["denied"]);
    // One that reaches the browser is the browser's to forget, when it comes.
    expect(await browsing.run(upload("given"), signal)).toEqual({ ok: "browser" });
    expect(unasked).toEqual([]);
    // One whose files are refused, or cannot be read; and one taken over while they were read.
    expect(await browsing.run(upload("no-files", []), signal)).toMatchObject({ error: { type: "browser" } });
    expect(await browsing.run(upload("gone", ["/home/u/notes/gone.txt"]), signal)).toMatchObject({ error: { type: "browser" } });
    reading.then = () => void browsing.takeOver(ROOT);
    expect(await browsing.run(upload("taken-over"), signal)).toEqual(PAUSED);
    reading.then = () => {};
    expect(unasked.splice(0)).toEqual(["no-files", "gone", "taken-over"]);
    // One that comes while its user holds the browser.
    expect(await browsing.run(upload("held"), signal)).toEqual(PAUSED);
    browsing.handBack(ROOT);
    expect(unasked.splice(0)).toEqual(["held"]);
    expect(browsed).toHaveLength(1);
    // One for a chat this computer did not bind, and one where no browser is here.
    expect(await rig(LAUNCH, false).browsing.run(upload("unbound"), signal)).toEqual(FOLDER_UNAVAILABLE);
    const none = rig(null);
    expect(await none.browsing.run(upload("no-browser"), signal)).toEqual(NO_BROWSER);
    expect(none.unasked).toEqual(["no-browser"]);
    // No other operation is an upload.
    expect(await none.browsing.run(op("browser.navigate"), signal)).toEqual(NO_BROWSER);
    expect(none.unasked).toEqual(["no-browser"]);
  });

  it("asks the browser for the address of the page a session acts in, and for an upload, of the file input that asked", async () => {
    expect(await rig().browsing.address("child")).toBe("https://example.com/child");
    expect(await rig().browsing.address("child", true)).toBe("https://example.com/child/the-input");
    // And which upload it is asked for.
    expect(await rig().browsing.address("child", true, "op-7")).toBe("https://example.com/child/the-input#op-7");
    // And for which chat.
    expect(await rig().browsing.address("child", true, "op-7", ROOT)).toBe(`https://example.com/child/the-input#op-7@${ROOT}`);
    // Where the browser says the upload can be given to nothing, that is its answer.
    expect(await rig().browsing.address("nowhere", true, "op-8")).toEqual({ refused: "no site" });
  });

  it("closes a deleted chat's tabs, and tells the tools beneath", () => {
    const { browsing, forgotten } = rig();
    browsing.retired(ROOT);
    expect(forgotten).toEqual([`browser ${ROOT}`, `tools ${ROOT}`]);
  });

  it("closes the browser with the tools at a stop, and when the computer's access ends", async () => {
    const { browsing, stopped } = rig();
    await browsing.end();
    await browsing.stop();
    expect(stopped).toEqual(["browser ended", "tools ended", "browser", "tools"]);
  });
});

describe("an upload's files, read as the chat's file host reads them", () => {
  // The file helper's own operations on a folder of the test's (files/operations.ts), as its sandboxed process
  // runs them for a chat's file host: without the sandbox, which answers no differently for a read.
  let base: string;
  let folder: string;
  beforeEach(() => {
    base = realpathSync(mkdtempSync(join(tmpdir(), "browsing-upload-")));
    folder = join(base, "folder");
    mkdirSync(join(folder, "sub"), { recursive: true });
    mkdirSync(join(base, "outside"));
    writeFileSync(join(folder, "a.txt"), "alpha\n");
    writeFileSync(join(base, "outside", "o.txt"), "outside\n");
  });
  afterEach(() => rmSync(base, { recursive: true, force: true }));
  const filed = () => {
    const made = rig(LAUNCH, true, (operation, stop) => perform(operation.kind, operation.args, { folder, home: "/home/tester", env: { PATH: "/usr/bin:/bin", HOME: "/home/tester" } }, stop));
    const upload = (...paths: string[]) => made.browsing.run({ ...op("browser.set_input_files"), args: { paths } }, signal);
    return { ...made, upload };
  };
  const b64 = (text: string) => Buffer.from(text).toString("base64");
  // A file of the folder that is *bytes* long, of which the disk holds none.
  const sparse = (name: string, bytes: number) => {
    writeFileSync(join(folder, name), "");
    truncateSync(join(folder, name), bytes);
  };

  it("gives a page a file of the chat's folder by its name, its type and what it holds", async () => {
    const { browsed, upload } = filed();
    writeFileSync(join(folder, "sub", "Scan 1.PNG"), "PNG");
    expect(await upload(`${folder}/a.txt`, `${folder}/sub/Scan 1.PNG`)).toEqual({ ok: "browser" });
    expect(browsed.at(-1)?.args).toEqual({ files: [
      { name: "a.txt", mimeType: "text/plain", buffer: b64("alpha\n") }, { name: "Scan 1.PNG", mimeType: "image/png", buffer: b64("PNG") },
    ] });
  });

  it("gives a page each file under its own last name, for every file its user was shown and no other", async () => {
    const { browsed, upload } = filed();
    // Names as a folder holds them: spaces, dots, a leading dash, quotes, another script, and as long as a name may be.
    const names = ["a b.txt", ".env", "..hidden", "-rf", "it's \"quoted\".csv", "日本語 файл.pdf", "a..b", "x.tar.gz", `${"n".repeat(251)}.txt`];
    mkdirSync(join(folder, "deep", "er"), { recursive: true });
    const paths = names.map((name, at) => join(folder, at % 2 === 0 ? "" : "deep/er", name));
    for (const path of paths) writeFileSync(path, `holds ${path}`);
    for (const some of [paths.slice(0, 5), paths.slice(5)]) {
      expect(await upload(...some)).toEqual({ ok: "browser" });
      const given = (browsed.at(-1)!.args as { files: Array<{ name: string; buffer: string }> }).files;
      // As many as were named, in their order, each by the last part of its own path and holding that file.
      expect(given.map((file) => file.name)).toEqual(some.map((path) => path.slice(path.lastIndexOf("/") + 1)));
      expect(given.map((file) => Buffer.from(file.buffer, "base64").toString())).toEqual(some.map((path) => `holds ${path}`));
    }
  });

  it("gives a page nothing its chat's file host does not read: nothing outside the folder, through a link, by a path that is not the file's own, or that is no file; and nothing at all of an upload that names one such", async () => {
    const { browsed, upload } = filed();
    symlinkSync(join(base, "outside", "o.txt"), join(folder, "to-outside"));
    symlinkSync("a.txt", join(folder, "to-inside"));
    symlinkSync(join(base, "outside"), join(folder, "out"));
    execFileSync("mkfifo", [join(folder, "pipe")]);
    sparse("large.bin", MAX_READ_BYTES + 1);
    const refused: Array<[string, string]> = [
      [`${base}/outside/o.txt`, `Not a path in this folder: '${base}/outside/o.txt'`],
      ["/etc/passwd", "Not a path in this folder: '/etc/passwd'"],
      [`${folder}/../outside/o.txt`, `Not a path in this folder: '${folder}/../outside/o.txt'`],
      [`${folder}/to-outside`, `Not a path in this folder: '${folder}/to-outside'`],
      [`${folder}/out/o.txt`, `Not a path in this folder: '${folder}/out/o.txt'`],
      // A link to a file of the folder's own is no name of that file: the file is named by its own path.
      [`${folder}/to-inside`, `Not a path in this folder: '${folder}/to-inside'`],
      ["a.txt", "Not a path in this folder: 'a.txt'"],
      [`${folder}/sub`, `Is a directory: '${folder}/sub'`],
      [`${folder}/pipe`, `Not a regular file: '${folder}/pipe'`],
      [`${folder}/missing.txt`, `No such file or directory: '${folder}/missing.txt'`],
      [`${folder}/large.bin`, "File too large to read from a local folder (over 50 MiB)"],
    ];
    for (const [key, why] of refused) {
      for (const paths of [[key], [`${folder}/a.txt`, key], [key, `${folder}/a.txt`]]) {
        expect(await upload(...paths), paths.join(" ")).toEqual({ error: { type: "browser", message: `${key} could not be read for the page: ${why}` } });
      }
    }
    expect(browsed).toEqual([]);
  });

  it("gives a page files of up to what a write may carry, in all, and none of an upload a byte over", async () => {
    const { browsed, upload } = filed();
    for (const name of ["half-1.bin", "half-2.bin"]) sparse(name, MAX_WRITE_BYTES / 2);
    writeFileSync(join(folder, "one.bin"), "1");
    expect(await upload(`${folder}/half-1.bin`, `${folder}/one.bin`, `${folder}/half-2.bin`)).toEqual({
      error: { type: "browser", message: "The files are too large to give the page at once: at most 52428800 bytes" },
    });
    expect(browsed).toEqual([]);
    expect(await upload(`${folder}/half-1.bin`, `${folder}/half-2.bin`)).toEqual({ ok: "browser" });
    const given = (browsed[0]?.args as { files: Array<{ buffer: string }> }).files;
    expect(given.map((file) => Buffer.byteLength(file.buffer, "base64"))).toEqual([MAX_WRITE_BYTES / 2, MAX_WRITE_BYTES / 2]);
  });

  it("reads a name the file host keeps from being written as it reads any file of the folder: an upload is a read, and the file host refuses no read by its name", async () => {
    const { browsed, upload } = filed();
    mkdirSync(join(folder, ".git"));
    writeFileSync(join(folder, ".git", "config"), "[remote]\n");
    writeFileSync(join(folder, ".env"), "KEY=value\n");
    expect(await upload(`${folder}/.git/config`, `${folder}/.env`)).toEqual({ ok: "browser" });
    // Each by its last name, and as plain data where its name says no more of what it is.
    expect((browsed[0]?.args as { files: Array<{ name: string; mimeType: string }> }).files.map((file) => [file.name, file.mimeType])).toEqual([
      ["config", "application/octet-stream"], [".env", "application/octet-stream"],
    ]);
  });
});
