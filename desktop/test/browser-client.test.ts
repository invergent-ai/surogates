import { type ChildProcess, fork } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import {
  BROWSER_HOST, BROWSER_STOPPED, BrowserClient, type BrowserProcess, CANCELLED, DUPLICATE, type FromBrowser, PAUSED, type ToBrowser,
} from "../src/browser/client.js";
import type { StagedDownload } from "../src/browser/downloads.js";
import { FILE_ASKED, NO_SITE, NOT_AS_ASKED } from "../src/browser/host.js";
import type { Operation } from "../src/link/protocol.js";
import { isolated, TEST_BROWSER } from "./isolated.js";

const operation = (id: string, kind = "browser.navigate", args: Record<string, unknown> = { url: "https://example.com/" }): Operation => ({
  id, sessionId: "root", callingSessionId: "child", invocationId: "call", ordinal: 1, kind, args, digest: `d-${id}`,
});
const LAUNCH = { executable: "/opt/google/chrome/chrome", profile: "/tmp/nowhere" };

// A host in a test: what it was sent, and its answers and exit as the test makes them.
class FakeHost implements BrowserProcess {
  readonly sent: ToBrowser[] = [];
  private listener: (message: FromBrowser) => void = () => {};
  private readonly exits: Array<() => void> = [];
  exited = false;
  killed = false;
  send(message: ToBrowser): void {
    this.sent.push(message);
  }
  onMessage(listener: (message: FromBrowser) => void): void {
    this.listener = listener;
  }
  onExit(listener: () => void): void {
    if (this.exited) listener();
    else this.exits.push(listener);
  }
  kill(): void {
    this.killed = true;
    this.exit();
  }
  say(message: FromBrowser): void {
    this.listener(message);
  }
  exit(): void {
    if (this.exited) return;
    this.exited = true;
    for (const listener of this.exits) listener();
  }
}

describe("the browser host's client", () => {
  it("starts a host at the first operation, sends it the calling session's operation, and gives its answer", async () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    const answered = client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    expect(hosts).toHaveLength(1);
    expect(hosts[0]!.sent).toEqual([{ type: "op", id: "op-1", launch: LAUNCH, root: "root", session: "child", kind: "browser.navigate", args: { url: "https://example.com/" } }]);
    hosts[0]!.say({ type: "result", id: "op-1", outcome: { ok: { url: "https://example.com/", title: "Example" } } });
    expect(await answered).toEqual({ ok: { url: "https://example.com/", title: "Example" } });
  });

  it("keeps a try's answer apart from an operation's, whatever the operation's id", async () => {
    const host = new FakeHost();
    const client = new BrowserClient(() => host);
    const ran = client.perform(LAUNCH, operation("try-1"), new AbortController().signal);
    const tried = client.tryBrowser("/opt/google/chrome/chrome");
    expect(host.sent.at(-1)).toEqual({ type: "try", id: "try-1", executable: "/opt/google/chrome/chrome" });
    host.say({ type: "result", id: "try-1", outcome: { ok: { url: "https://example.com/", title: "Example" } } });
    host.say({ type: "tried", id: "try-1", outcome: { ok: { version: "154.0" } } });
    expect(await tried).toEqual({ ok: { version: "154.0" } });
    expect(await ran).toEqual({ ok: { url: "https://example.com/", title: "Example" } });
  });

  it("tells a running host to close a deleted chat's tabs, and starts none to do it", () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    client.forget("root");
    expect(hosts).toHaveLength(0);
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    client.forget("root");
    expect(hosts[0]!.sent.at(-1)).toEqual({ type: "forget", root: "root" });
  });

  it("tells a running host of a chat taken over and handed back, and starts none to do it", () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    client.pause("root", true);
    expect(hosts).toEqual([]);
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    client.pause("root", true);
    client.pause("root", false);
    expect(hosts[0]!.sent.slice(1)).toEqual([{ type: "pause", root: "root", paused: true }, { type: "pause", root: "root", paused: false }]);
  });

  it("tells a running host of an upload its user was asked about that is not coming, and starts none to do it", () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    client.notComing("op-7");
    expect(hosts).toHaveLength(0);
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    client.notComing("op-7");
    expect(hosts[0]!.sent.at(-1)).toEqual({ type: "not_coming", of: "op-7" });
  });

  it("asks a running host to show a chat's page, and says none is shown where no host runs", async () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    expect(await client.show("root")).toBe(false);
    expect(hosts).toEqual([]);
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    const showing = client.show("root");
    const asked = hosts[0]!.sent.at(-1) as Extract<ToBrowser, { type: "show" }>;
    expect(asked).toEqual({ type: "show", id: asked.id, root: "root" });
    hosts[0]!.say({ type: "shown", id: asked.id, shown: true });
    expect(await showing).toBe(true);
    // A host that goes shows nothing.
    const again = client.show("root");
    hosts[0]!.exit();
    expect(await again).toBe(false);
  });

  it("says no page is shown when the host does not say in time, or is stopping, and keeps nothing of a show once it is answered", async () => {
    vi.useFakeTimers();
    try {
      const host = new FakeHost();
      const client = new BrowserClient(() => host);
      const showing = (client as unknown as { showing: Map<string, unknown> }).showing;
      void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
      // A host that never answers leaves nobody waiting.
      const unanswered = client.show("root");
      await vi.advanceTimersByTimeAsync(4_999);
      expect(showing.size).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(await unanswered).toBe(false);
      expect(showing.size).toBe(0);
      // One it answers is kept no longer either, and its late bound answers nothing.
      const answered = client.show("root");
      host.say({ type: "shown", id: (host.sent.at(-1) as Extract<ToBrowser, { type: "show" }>).id, shown: true });
      expect(await answered).toBe(true);
      expect(showing.size).toBe(0);
      expect(vi.getTimerCount()).toBe(0);
      // A host being stopped is asked nothing more.
      void client.stop();
      const sent = host.sent.length;
      expect(await client.show("root")).toBe(false);
      expect(host.sent).toHaveLength(sent);
      host.say({ type: "stopped" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("hands each download its host staged to its listener", () => {
    const host = new FakeHost();
    const client = new BrowserClient(() => host);
    const heard: unknown[] = [];
    client.onDownload((download) => heard.push(download));
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    const download = { root: "root", session: "child", name: "report.txt", path: "/tmp/nowhere/a", user: false };
    host.say({ type: "download", ...download });
    host.say({ type: "download", ...download, user: true });
    // Its user's only where the host says exactly so.
    host.say({ type: "download", ...download, user: "yes" as unknown as boolean });
    host.say({ type: "download", root: "root", session: "child", name: "report.txt", path: "/tmp/nowhere/a" } as FromBrowser);
    expect(heard).toEqual([download, { ...download, user: true }, download, download]);
    // And theirs only by the minute after a hand back, of which the agent is told, only where the host says exactly that.
    heard.length = 0;
    host.say({ type: "download", ...download, user: true, afterHandBack: true });
    host.say({ type: "download", ...download, user: true, afterHandBack: "yes" as unknown as true });
    host.say({ type: "download", ...download, user: true });
    expect(heard).toEqual([{ ...download, user: true, afterHandBack: true }, { ...download, user: true }, { ...download, user: true }]);
    expect(heard.map((staged) => Object.hasOwn(staged as object, "afterHandBack"))).toEqual([true, false, false]);
  });

  it("asks a running host for the address of a session's page, and says a new tab's where none runs", async () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    // No host: the session's next operation opens a new tab, in a browser it starts.
    expect(await client.address("child")).toBe("about:blank");
    expect(hosts).toHaveLength(0);
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    const asked = client.address("child");
    const sent = hosts[0]!.sent.at(-1) as { type: string; id: string; session: string };
    expect(sent).toMatchObject({ type: "address", session: "child" });
    hosts[0]!.say({ type: "address", id: sent.id, url: "https://bank.example/" });
    expect(await asked).toBe("https://bank.example/");
    // For an upload: the frame of the file input the session's page asked for.
    void client.address("child", true);
    expect(hosts[0]!.sent.at(-1)).toMatchObject({ type: "address", session: "child", upload: true });
    expect(hosts[0]!.sent.at(-1)).not.toHaveProperty("of");
    // And which upload it is asked for, by its operation: the input named is that upload's alone.
    void client.address("child", true, "op-7");
    expect(hosts[0]!.sent.at(-1)).toMatchObject({ type: "address", session: "child", upload: true, of: "op-7" });
    // A host that says an upload can be given to nothing says why, and that is its answer.
    const refusing = client.address("child", true, "op-9");
    const asked9 = hosts[0]!.sent.at(-1) as { id: string };
    hosts[0]!.say({ type: "address", id: asked9.id, url: "about:blank", refused: "runs as no site" });
    expect(await refusing).toEqual({ refused: "runs as no site" });
    // No other act's address names an upload.
    void client.address("child", false, "op-8");
    expect(Object.keys(hosts[0]!.sent.at(-1)!).sort()).toEqual(["id", "session", "type"]);
    // The chat that asks goes with it: the host says a session's page only to its own chat.
    void client.address("child", true, "op-9", "root");
    expect(hosts[0]!.sent.at(-1)).toMatchObject({ type: "address", session: "child", upload: true, of: "op-9", root: "root" });
    void client.address("child", false, undefined, "root");
    expect(hosts[0]!.sent.at(-1)).toMatchObject({ type: "address", session: "child", root: "root" });
    // A host that goes with one asked: the next operation opens a new tab.
    const pending = client.address("child");
    hosts[0]!.exit();
    expect(await pending).toBe("about:blank");
  });

  it("refuses a second operation under the id of one still running, and sends the host only the first", async () => {
    const host = new FakeHost();
    const client = new BrowserClient(() => host);
    const first = client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    expect(await client.perform(LAUNCH, operation("op-1", "browser.evaluate", { code: "return 1;" }), new AbortController().signal)).toEqual(DUPLICATE);
    expect(host.sent.filter((message) => message.type === "op")).toHaveLength(1);
    host.say({ type: "result", id: "op-1", outcome: { ok: { url: "https://example.com/", title: "Example" } } });
    expect(await first).toEqual({ ok: { url: "https://example.com/", title: "Example" } });
  });

  it("closes nothing, and starts no host, for a close where no host runs", async () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    expect(await client.perform(LAUNCH, operation("op-1", "browser.close", {}), new AbortController().signal)).toEqual({ ok: { closed: false } });
    expect(hosts).toHaveLength(0);
  });

  it("answers a cancel at once and tells the host", async () => {
    const host = new FakeHost();
    const client = new BrowserClient(() => host);
    const cancel = new AbortController();
    const answered = client.perform(LAUNCH, operation("op-1"), cancel.signal);
    cancel.abort();
    expect(await answered).toEqual(CANCELLED);
    expect(host.sent.at(-1)).toEqual({ type: "cancel", id: "op-1" });
  });

  it("answers what a host that went was running as interrupted, and starts another for the next", async () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    const running = client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    hosts[0]!.exit();
    expect(await running).toEqual(BROWSER_STOPPED);
    void client.perform(LAUNCH, operation("op-2"), new AbortController().signal);
    expect(hosts).toHaveLength(2);
  });

  it("stops its host, which closes the browser, and runs nothing after", async () => {
    const host = new FakeHost();
    const client = new BrowserClient(() => host);
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    const stopped = client.stop();
    expect(host.sent.at(-1)).toEqual({ type: "stop" });
    host.say({ type: "stopped" });
    await stopped;
    expect(host.killed).toBe(true);
    expect(await client.perform(LAUNCH, operation("op-2"), new AbortController().signal)).toEqual(BROWSER_STOPPED);
  });

  it("ends its host when the computer's access ends, and a later operation starts one again", async () => {
    const hosts: FakeHost[] = [];
    const client = new BrowserClient(() => {
      hosts.push(new FakeHost());
      return hosts.at(-1)!;
    });
    void client.perform(LAUNCH, operation("op-1"), new AbortController().signal);
    const ended = client.end();
    hosts[0]!.say({ type: "stopped" });
    await ended;
    void client.perform(LAUNCH, operation("op-2"), new AbortController().signal);
    expect(hosts).toHaveLength(2);
  });
});

const EXECUTABLE = TEST_BROWSER;
const run = EXECUTABLE !== undefined && process.env.SUROGATE_BROWSER_TESTS === "1";

let profile = "";
afterEach(() => {
  if (profile) rmSync(profile, { recursive: true, force: true });
  profile = "";
});

const browserOf = (path: string) => readdirSync("/proc").filter((pid) => /^\d+$/.test(pid)).filter((pid) => {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, "utf8").includes(path);
  } catch {
    return false;
  }
});

// The real host, a Node child process of the test's own, behind SUROGATE_BROWSER_TESTS=1 and apart from the user's session, as browser-host.test.ts says.
describe.skipIf(!run)("the browser host's process", () => {
  beforeAll(() => isolated());

  it("takes its browser with it when it is killed, and its running operation is answered interrupted", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profile-"));
    let child: ChildProcess | undefined;
    const client = new BrowserClient(() => {
      child = fork(BROWSER_HOST, [], { stdio: ["ignore", 2, 2, "ipc"] });
      const exits: Array<() => void> = [];
      let closed = false;
      child.once("close", () => {
        closed = true;
        for (const listener of exits) listener();
      });
      return {
        send: (message) => void child!.send(message),
        onMessage: (listener) => void child!.on("message", (message) => listener(message as FromBrowser)),
        onExit: (listener) => (closed ? listener() : void exits.push(listener)),
        kill: () => void child!.kill("SIGKILL"),
      };
    });
    const launch = { executable: EXECUTABLE!, profile };
    const signal = new AbortController().signal;
    // This computer's own address: refused by the proxy, but the browser is up, with a tab.
    expect(await client.perform(launch, operation("op-1", "browser.navigate", { url: "http://127.0.0.1:9/" }), signal)).toMatchObject({ error: { type: "browser" } });
    expect(browserOf(profile).length).toBeGreaterThan(0);
    const running = client.perform(launch, operation("op-2", "browser.evaluate", { code: "await new Promise((r) => setTimeout(r, 60000));" }), signal);
    await new Promise((done) => setTimeout(done, 500));
    child!.kill("SIGKILL");
    expect(await running).toEqual(BROWSER_STOPPED);
    // Its pipe closed, the browser closes itself: Edge takes about 5 s to, as at its own close.
    await expect.poll(() => browserOf(profile).length, { timeout: 10_000 }).toBe(0);
  });

  it("runs no second operation sent under the id of one it is still running", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profile-"));
    let child: ChildProcess | undefined;
    const client = new BrowserClient(() => {
      child = fork(BROWSER_HOST, [], { stdio: ["ignore", 2, 2, "ipc"] });
      const exits: Array<() => void> = [];
      let closed = false;
      child.once("close", () => {
        closed = true;
        for (const listener of exits) listener();
      });
      return {
        send: (message) => void child!.send(message),
        onMessage: (listener) => void child!.on("message", (message) => listener(message as FromBrowser)),
        onExit: (listener) => (closed ? listener() : void exits.push(listener)),
        kill: () => void child!.kill("SIGKILL"),
      };
    });
    const launch = { executable: EXECUTABLE!, profile };
    const signal = new AbortController().signal;
    try {
      await client.perform(launch, operation("op-1", "browser.navigate", { url: "http://127.0.0.1:9/" }), signal);
      const running = client.perform(launch, operation("op-2", "browser.evaluate", { code: "await new Promise((r) => setTimeout(r, 1500)); return 'first';" }), signal);
      await new Promise((done) => setTimeout(done, 200));
      // Past the client's own check, as a client that lost track of its ids would send it.
      child!.send({ ...operation("op-2"), type: "op", launch, root: "root", session: "child", kind: "browser.evaluate", args: { code: "document.title = 'second ran'; return 'second';" } });
      expect(await running).toEqual({ ok: { value: "first" } });
      await new Promise((done) => setTimeout(done, 500));
      expect(await client.perform(launch, operation("op-3", "browser.evaluate", { code: "return document.title;" }), signal)).not.toEqual({ ok: { value: "second ran" } });
    } finally {
      await client.stop();
    }
  });

  it("hears of a chat taken over and handed back, and is asked to show its page, through the host's own process", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profile-"));
    const client = new BrowserClient();
    const launch = { executable: EXECUTABLE!, profile };
    const signal = new AbortController().signal;
    // What the host answers within 5 s, or "no answer": a message it does not handle is never answered.
    const answered = (shown: Promise<boolean>) => Promise.race([shown, new Promise<string>((done) => setTimeout(() => done("no answer"), 5_000))]);
    try {
      // This computer's own address: refused by the proxy, but the browser is up, with a tab.
      await client.perform(launch, operation("op-1", "browser.navigate", { url: "http://127.0.0.1:9/" }), signal);
      // One that holds its page a moment, and one waiting behind it in the session's line.
      const holding = client.perform(launch, operation("op-2", "browser.evaluate", { code: "await new Promise((r) => setTimeout(r, 1500)); return 'held';" }), signal);
      const waiting = client.perform(launch, operation("op-3", "browser.evaluate", { code: "document.title = 'ran after the pause'; return 'waited';" }), signal);
      await new Promise((done) => setTimeout(done, 300));
      client.pause("root", true);
      // The one acting is answered paused, with nothing it read after; the one waiting never acts.
      expect(await holding).toEqual(PAUSED);
      expect(await waiting).toEqual(PAUSED);
      expect(await client.perform(launch, operation("op-4", "browser.close", {}), signal)).toEqual(PAUSED);
      expect(await answered(client.show("root"))).toBe(true);
      expect(await answered(client.show("another-chat"))).toBe(false);
      client.pause("root", false);
      // Handed back, the chat's operations run again, in a page the waiting one never acted in.
      expect(await client.perform(launch, operation("op-5", "browser.evaluate", { code: "return document.title === 'ran after the pause';" }), signal)).toEqual({ ok: { value: false } });
    } finally {
      await client.stop();
    }
  }, 30_000);

  it("gives a page what an upload's files hold, and an upload its user was asked about to nothing but what its own prompt named, through the host's own process", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profile-"));
    const client = new BrowserClient();
    const launch = { executable: EXECUTABLE!, profile };
    const signal = new AbortController().signal;
    let ops = 0;
    const sent = (kind: string, args: Record<string, unknown>, id = `op-${(ops += 1)}`) =>
      client.perform(launch, operation(id, kind, args), signal) as Promise<{ ok?: any; error?: { type: string; message: string } }>;
    const files = [{ name: "report.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7").toString("base64") }];
    // The page makes itself a new file input and has it ask, as a page's script can: settled once the host has heard it.
    const asks = async () => {
      await sent("browser.evaluate", {
        code: "document.querySelector('input')?.remove(); const input = document.createElement('input'); input.type = 'file'; document.body.append(input); input.click(); return 1;",
      });
      await expect.poll(async () => (await sent("browser.mouse", { action: "move", x: 1, y: 1 })).ok?.notices, { timeout: 10_000 }).toEqual([FILE_ASKED]);
    };
    const holds = async () => (await sent("browser.evaluate", {
      code: "const [file] = document.querySelector('input').files; return file ? [file.name, file.type, await file.text()] : null;",
    })).ok?.value;
    try {
      // This computer's own address: refused by the proxy, but the browser is up, with a tab: its error page, which runs as no site.
      await sent("browser.navigate", { url: "http://127.0.0.1:9/" });
      // An upload nobody was asked about, as in a chat that works freely: its files reach the input whole.
      await asks();
      expect(await sent("browser.set_input_files", { files })).toEqual({ ok: { files: 1, notices: [] } });
      expect(await holds()).toEqual(["report.pdf", "application/pdf", "%PDF-1.7"]);
      // One its user is asked about: the host names the input for that operation, and the operation finds what was named for it.
      // Here that is an input in a page that runs as no site, so it is told so, and given nothing.
      await asks();
      expect(await client.address("child", true, "asked-first")).toEqual({ refused: NO_SITE });
      expect((await sent("browser.set_input_files", { files }, "asked-first")).error?.message).toBe(NO_SITE);
      expect(await holds()).toBeNull();
      // One asked about, whose agent acted before its files came: given nothing. One nobody was asked about then goes to what asked last.
      await asks();
      await client.address("child", true, "asked-second");
      await sent("browser.evaluate", { code: "return 1;" });
      expect((await sent("browser.set_input_files", { files }, "asked-second")).error?.message).toBe(NOT_AS_ASKED);
      expect(await holds()).toBeNull();
      expect(await sent("browser.set_input_files", { files }, "asked-by-nobody")).toMatchObject({ ok: { files: 1 } });
      expect(await holds()).toEqual(["report.pdf", "application/pdf", "%PDF-1.7"]);
      // The host says a session's page only to the chat it is of: the chat that asks goes with the question.
      expect(await client.address("child", false, undefined, "root")).toMatch(/^chrome-error:/);
      expect(await client.address("child", false, undefined, "another-chat")).toBe("about:blank");
      // One asked about and denied: the host is told it is not coming, and knows it no more as one it was asked about.
      // Sent all the same, as only a mistake of this computer's own could, it is one nobody was asked about.
      await asks();
      await client.address("child", true, "asked-third");
      client.notComing("asked-third");
      await sent("browser.evaluate", { code: "return 1;" });
      expect(await sent("browser.set_input_files", { files }, "asked-third")).toMatchObject({ ok: { files: 1 } });
    } finally {
      await client.stop();
    }
  }, 60_000);

  it("hands a download a page finished to its client's listener through the host's own process, staged in the host's temporary folder until its browser closes", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profile-"));
    const client = new BrowserClient();
    const heard: StagedDownload[] = [];
    client.onDownload((download) => heard.push(download));
    const launch = { executable: EXECUTABLE!, profile };
    const signal = new AbortController().signal;
    try {
      // This computer's own address: refused by the proxy, but the browser is up, with a tab.
      await client.perform(launch, operation("op-1", "browser.navigate", { url: "http://127.0.0.1:9/" }), signal);
      // No site is reached from here: the page makes its file itself, as a page's script can.
      const code = "const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['report'])); a.download = 'report.txt'; a.click(); return 1;";
      expect(await client.perform(launch, operation("op-2", "browser.evaluate", { code }), signal)).toEqual({ ok: { value: 1 } });
      await expect.poll(() => heard.length, { timeout: 10_000 }).toBe(1);
      expect(heard[0]).toEqual({ root: "root", session: "child", name: "report.txt", path: heard[0]!.path, user: false });
      expect(readFileSync(heard[0]!.path, "utf8")).toBe("report");
      // Under the host's own temporary folder, apart from the profile.
      expect([heard[0]!.path.startsWith(`${tmpdir()}/`), heard[0]!.path.startsWith(profile)]).toEqual([true, false]);
      // Taken over and handed back: the same download, just after, comes as its user's by that alone, and says so.
      client.pause("root", true);
      client.pause("root", false);
      expect(await client.perform(launch, operation("op-3", "browser.evaluate", { code }), signal)).toEqual({ ok: { value: 1 } });
      await expect.poll(() => heard.length, { timeout: 10_000 }).toBe(2);
      expect(heard[1]).toEqual({ root: "root", session: "child", name: "report.txt", path: heard[1]!.path, user: true, afterHandBack: true });
      // And one that comes while they hold the browser, as theirs outright.
      client.pause("root", true);
      await new Promise((done) => setTimeout(done, 300));
      expect(await client.perform(launch, operation("op-4", "browser.evaluate", { code }), signal)).toEqual(PAUSED);
    } finally {
      await client.stop();
    }
    // What nobody saved goes with the browser.
    expect(existsSync(heard[0]!.path)).toBe(false);
  }, 30_000);

  it("clears what a host that was killed left staged when the next host starts its browser, and leaves what a running host has staged", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profiles-"));
    const signal = new AbortController().signal;
    const code = "const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['report'])); a.download = 'report.txt'; a.click(); return 1;";
    // A host with a download staged, by its process and what it staged.
    const withStaged = async (name: string) => {
      let child: ChildProcess | undefined;
      const client = new BrowserClient(() => {
        child = fork(BROWSER_HOST, [], { stdio: ["ignore", 2, 2, "ipc"] });
        const exits: Array<() => void> = [];
        child.once("close", () => exits.forEach((listener) => listener()));
        return {
          send: (message) => void child!.send(message, () => {}),
          onMessage: (listener) => void child!.on("message", (message) => listener(message as FromBrowser)),
          onExit: (listener) => void exits.push(listener),
          kill: () => void child!.kill("SIGKILL"),
        };
      });
      const heard: StagedDownload[] = [];
      client.onDownload((download) => heard.push(download));
      const launch = { executable: EXECUTABLE!, profile: join(profile, name) };
      await client.perform(launch, operation(`${name}-1`, "browser.navigate", { url: "http://127.0.0.1:9/" }), signal);
      await client.perform(launch, operation(`${name}-2`, "browser.evaluate", { code }), signal);
      await expect.poll(() => heard.length, { timeout: 10_000 }).toBe(1);
      return { client, child: child!, staged: heard[0]!.path };
    };
    const killed = await withStaged("first");
    const running = await withStaged("second");
    try {
      // Killed, with no word: its browser goes with it, and what it staged stays.
      const gone = new Promise((done) => killed.child.once("exit", done));
      killed.child.kill("SIGKILL");
      await gone;
      expect([existsSync(killed.staged), existsSync(running.staged)]).toEqual([true, true]);
      // The next host to start its browser in that temporary folder clears it, and nothing of the host that still runs.
      const next = await withStaged("third");
      try {
        expect([existsSync(killed.staged), existsSync(dirname(killed.staged)), existsSync(running.staged), existsSync(next.staged)]).toEqual([false, false, true, true]);
      } finally {
        await next.client.stop();
      }
      // A host that stops takes its own with it.
      expect(existsSync(dirname(next.staged))).toBe(false);
    } finally {
      await running.client.stop();
    }
  }, 60_000);

  it("closes its headed browser when it is stopped, as the app's quit stops it", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profile-"));
    const client = new BrowserClient();
    const launch = { executable: EXECUTABLE!, profile };
    expect(await client.perform(launch, operation("op-1", "browser.navigate", { url: "http://127.0.0.1:9/" }), new AbortController().signal)).toMatchObject({ error: { type: "browser" } });
    expect(browserOf(profile).length).toBeGreaterThan(0);
    await client.stop();
    expect(browserOf(profile)).toEqual([]);
  });

  it("leaves nothing writing its profile once it is stopped, so that a log out's removal right after forgets it whole", async () => {
    profile = mkdtempSync(join(tmpdir(), "sb-profiles-"));
    const client = new BrowserClient();
    const launch = { executable: EXECUTABLE!, profile: join(profile, "browser") };
    const signal = new AbortController().signal;
    await client.perform(launch, operation("op-1", "browser.navigate", { url: "http://127.0.0.1:9/" }), signal);
    await client.perform(launch, operation("op-2", "browser.evaluate", { code: "await new Promise((r) => setTimeout(r, 1500)); return 1;" }), signal);
    await client.stop();
    rmSync(profile, { recursive: true, force: true });
    // As the browser would write it again, if any of it were still running.
    await new Promise((done) => setTimeout(done, 2_000));
    expect(existsSync(profile)).toBe(false);
  });

  it("launches a browser the user picked once, headless, and says its version", async () => {
    const client = new BrowserClient();
    try {
      expect((await client.tryBrowser(EXECUTABLE!) as { ok: { version: string } }).ok.version).toMatch(/^\d+\./);
      expect(await client.tryBrowser("/bin/true")).toMatchObject({ error: { type: "browser" } });
    } finally {
      await client.stop();
    }
  });
});
