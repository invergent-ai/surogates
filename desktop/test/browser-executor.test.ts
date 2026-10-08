import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PAUSED } from "../src/browser/client.js";
import { interrupted, type StagedDownload, UNSAVED } from "../src/browser/downloads.js";
import { Browsing, NO_BROWSER } from "../src/browser/executor.js";
import type { Launch } from "../src/browser/host.js";
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

function rig(launch: Launch | null = LAUNCH, bound = true) {
  const ran: string[] = [];
  const browsed: Array<{ launch: Launch; kind: string }> = [];
  const stopped: string[] = [];
  const forgotten: string[] = [];
  const paused: Array<[string, boolean]> = [];
  const shown: string[] = [];
  // What the browser's next answer is, and how it tells of a download it staged.
  const answers: Outcome[] = [];
  let staged: (download: StagedDownload) => void = () => {};
  // The chats this computer bound, until one is deleted.
  const chats = new Set(bound ? [ROOT, OTHER] : []);
  const tools: ToolLayer = {
    run: (operation) => (ran.push(operation.kind), Promise.resolve({ ok: "tools" })),
    refusal: () => ({ error: { type: "other", message: "from the tools" } }),
    guards: () => ({ home: "/home/u", dataDir: "/data", appDirs: [] }),
    live: () => [],
    stop: () => (stopped.push("tools"), Promise.resolve()),
    end: () => (stopped.push("tools ended"), Promise.resolve()),
    retired: (root) => void forgotten.push(`tools ${root}`),
  };
  const browsing = new Browsing({
    tools,
    browser: {
      perform: (chosen, operation) => (browsed.push({ launch: chosen, kind: operation.kind }), Promise.resolve<Outcome>(answers.shift() ?? { ok: "browser" })),
      forget: (root) => void forgotten.push(`browser ${root}`),
      stop: () => (stopped.push("browser"), Promise.resolve()),
      end: () => (stopped.push("browser ended"), Promise.resolve()),
      address: (session) => Promise.resolve(`https://example.com/${session}`),
      pause: (root, held) => void paused.push([root, held]),
      show: (root) => (shown.push(root), Promise.resolve(true)),
      onDownload: (listener) => {
        staged = listener;
      },
    },
    bindingOf: (root) => (chats.has(root) ? {} : undefined),
    launch: () => launch,
    staging,
  });
  return { browsing, ran, browsed, stopped, forgotten, paused, shown, chats, answers, stage: (download: StagedDownload) => staged(download) };
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

  it("asks the browser for the address of the page a session acts in", async () => {
    expect(await rig().browsing.address("child")).toBe("https://example.com/child");
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
