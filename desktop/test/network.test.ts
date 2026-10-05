import { mkdirSync, mkdtempSync, realpathSync, rmSync, statSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { networkInterfaces, tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type ApprovalAnswer, type ApprovalPrompts, type ApprovalRequest, Approvals } from "../src/binding/approvals.js";
import { BOOT_ID } from "../src/binding/folder.js";
import type { HostStart } from "../src/hosts/messages.js";
import { ToolHosts } from "../src/hosts/tool-hosts.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { Operation } from "../src/link/protocol.js";
import { bound, Harness, PACKAGE } from "./host-harness.js";

type Answer = { ok?: { output: string; returncode: number; timed_out: boolean }; error?: { type: string; message: string } };

let base: string;
let folder: string;
let start: HostStart;
let harnesses: Harness[];
let server: Server;
let port: number;
let requests: string[];
let next = 0;

async function host(overrides: Partial<HostStart> = {}): Promise<Harness> {
  const harness = new Harness();
  harnesses.push(harness);
  harness.send({ ...start, ...overrides });
  await harness.until((messages) => messages.find((message) => message.type === "ready"));
  return harness;
}

const op = (harness: Harness, kind: string, args: Record<string, unknown>) => harness.op(`op-${next++}`, kind, args) as Promise<Answer>;
const run = (harness: Harness, command: string, timeout = 20) => op(harness, "run", { command, workdir: null, timeout });
const asks = (harness: Harness) => harness.messages.filter((message) => message.type === "ask");
// Destinations no host answers (TEST-NET-1, RFC 5737): what srt lets through times out
// at curl's 2 s, or fails at once with srt's 502 on a computer with no route, and never
// gets srt's 403; what it refuses gets the 403 before anything is dialed.
const AWAY = "192.0.2.1";
const NEXT_DOOR = "192.0.2.2";
const PASSED = /^(000|502)\n$/;
// The HTTP status a command gets, alone. --noproxy '' sends a destination in srt's NO_PROXY (loopback, the private ranges) to srt's proxy.
const code = (url: string, flags = "") => `curl -sS --max-time 2 --noproxy '' ${flags} -o /dev/null -w '%{http_code}\\n' ${url} 2>/dev/null`;
const refusal = (destination: string) => `This computer did not allow network access to ${destination}.`;
const waiting = (destination: string) => `Still waiting for this computer's user to allow network access to ${destination}.`;

beforeEach(async () => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "network-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  mkdirSync(join(base, "home"));
  start = {
    type: "start",
    folder,
    expect: bound(folder),
    domains: [],
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    env: { HOME: join(base, "home"), LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    appDirs: [dirname(process.execPath), PACKAGE],
  };
  harnesses = [];
  requests = [];
  // This computer's own service, on every address it has: nothing should reach it.
  server = createServer((request, response) => {
    requests.push(request.url ?? "");
    response.end(`hello ${request.url}\n`);
  });
  await new Promise<void>((resolve) => server.listen(0, "::", resolve));
  port = (server.address() as AddressInfo).port;
});

afterEach(async () => {
  for (const harness of harnesses) await harness.stop();
  await new Promise((resolve) => server.close(resolve));
  rmSync(base, { recursive: true, force: true });
});

describe("a command's connection to a destination off the package hosts", { timeout: 60_000 }, () => {
  it("asks the app, waits for its answer, and goes through once allowed", async () => {
    const harness = await host();
    const answer = run(harness, code(`http://${AWAY}:9/`));
    expect(await harness.answer(true)).toEqual({ type: "ask", id: 1, host: AWAY, port: 9, privateNetwork: false });
    expect((await answer).ok?.output).toMatch(PASSED);
  });

  it("is refused when the app denies it, and the agent is told once, in that command's output", async () => {
    const harness = await host();
    const answer = run(harness, code(`http://${AWAY}:9/`));
    await harness.answer(false);
    expect((await answer).ok?.output).toBe(`403\n\n${refusal(`${AWAY}:9`)}`);
    expect((await run(harness, "echo next")).ok?.output).toBe("next\n");
  });

  it("asks once for the connections to one destination in flight, and again for one after the answer", async () => {
    const harness = await host();
    const answer = run(harness, `for i in $(seq 20); do ${code(`http://${AWAY}:9/$i`)} & done; wait`);
    await harness.until((messages) => messages.find((message) => message.type === "ask"));
    // Long enough for all 20 to reach srt's proxy and wait there.
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    await harness.answer(false);
    expect((await answer).ok?.output).toBe(`${"403\n".repeat(20)}\n${refusal(`${AWAY}:9`)}`);
    expect(asks(harness)).toHaveLength(1);
    const again = run(harness, code(`http://${AWAY}:9/`));
    await harness.answer(false);
    expect((await again).ok?.output).toBe(`403\n\n${refusal(`${AWAY}:9`)}`);
    expect(asks(harness)).toHaveLength(2);
  });

  it("lets a host allowed for the session through from then on, on every port, in a runner already up, without asking", async () => {
    const harness = await host();
    // A background process: from now on every command runs in the session runner, wrapped before the grant.
    const started = (await op(harness, "start", {
      command: "sleep 694", workdir: null, task_id: "t", pty: false, notify_on_complete: false, watcher_interval: null,
    })) as unknown as { ok: { session_id: string } };
    const first = run(harness, code(`http://${AWAY}:9/`));
    await harness.answer(true, true);
    expect((await first).ok?.output).toMatch(PASSED);
    expect((await run(harness, code(`http://${AWAY}:7/`))).ok?.output).toMatch(PASSED);
    expect(asks(harness)).toHaveLength(1);
    // Another host still asks.
    const other = run(harness, code(`http://${NEXT_DOOR}:9/`));
    expect(await harness.answer(false)).toMatchObject({ host: NEXT_DOOR, port: 9 });
    expect((await other).ok?.output).toBe(`403\n\n${refusal(`${NEXT_DOOR}:9`)}`);
    await op(harness, "kill", { session_id: started.ok.session_id });
  });

  it("starts with the hosts the chat's user allowed for the chat, and asks about none of them", async () => {
    const harness = await host({ domains: [AWAY] });
    expect((await run(harness, code(`http://${AWAY}:9/`))).ok?.output).toMatch(PASSED);
    expect(asks(harness)).toEqual([]);
  });

  const lan = Object.values(networkInterfaces()).flat().find((entry) => entry && !entry.internal && entry.family === "IPv4")?.address;
  it.skipIf(!lan)("leaves out an address granted for the chat that is this computer's own now", async () => {
    const address = lan ?? "";
    const harness = await host({ domains: [address] });
    expect((await run(harness, code(`http://${address}:${port}/`))).ok?.output).toBe(
      `403\n\nThis computer does not let a chat reach its own network services (${address}:${port})`,
    );
    expect([asks(harness), requests]).toEqual([[], []]);
  });

  it("keeps a command's own timeout running while its connection waits for the app", async () => {
    const harness = await host();
    const answer = run(harness, `curl -sS -o /dev/null http://${AWAY}:9/`, 5);
    await harness.until((messages) => messages.find((message) => message.type === "ask"));
    expect((await answer).ok).toEqual({
      output: `Command timed out after 5 seconds\n${waiting(`${AWAY}:9`)}`, returncode: 124, timed_out: true,
    });
  });

  it("tells a command that ends while its connection waits for the app, once, and the next that it was refused", async () => {
    const harness = await host();
    const waited = await run(harness, `curl -sS -o /dev/null http://${AWAY}:9/`, 5);
    expect(waited.ok?.output).toBe(`Command timed out after 5 seconds\n${waiting(`${AWAY}:9`)}`);
    // Still waiting, and already told.
    expect((await run(harness, "echo next")).ok?.output).toBe("next\n");
    // The prompt is dismissed, which denies.
    await harness.answer(false);
    expect((await run(harness, "echo after")).ok?.output).toBe(`after\n\n${refusal(`${AWAY}:9`)}`);
  });

  it("names at most 20 destinations of a kind in a notice, then how many more", async () => {
    const harness = await host();
    const answer = run(harness, `for port in $(seq 25); do ${code(`http://${AWAY}:$port/`)}; done`);
    for (let i = 0; i < 25; i += 1) await harness.answer(false);
    const named = Array.from({ length: 20 }, (_, i) => `${AWAY}:${i + 1}`).join(", ");
    expect((await answer).ok?.output).toBe(`${"403\n".repeat(25)}\nThis computer did not allow network access to ${named} and 5 more.`);
  });

  it("refuses this computer's own services without asking, however a command spells them, and tells the agent once", async () => {
    const harness = await host();
    const urls = [
      `http://127.0.0.1:${port}/`, `http://localhost:${port}/`, `http://LocalHost.:${port}/`, `http://[::1]:${port}/`,
      ...(lan ? [`http://${lan}:${port}/`] : []),
    ];
    const refused = [`127.0.0.1:${port}`, `localhost:${port}`, `[::1]:${port}`, ...(lan ? [`${lan}:${port}`] : [])];
    const answer = await run(harness, urls.map((url) => code(url)).join("; "));
    expect(answer.ok?.output).toBe(
      `${"403\n".repeat(urls.length)}\nThis computer does not let a chat reach its own network services (${refused.join(", ")})`,
    );
    expect([asks(harness), requests]).toEqual([[], []]);
    expect((await run(harness, "echo next")).ok?.output).toBe("next\n");
  });

  it("asks about a private network saying so, and refuses a name it cannot look up without asking", async () => {
    const harness = await host();
    const inside = run(harness, code("http://10.255.255.1:9/"));
    expect(await harness.answer(false)).toMatchObject({ host: "10.255.255.1", port: 9, privateNetwork: true });
    expect((await inside).ok?.output).toBe(`403\n\n${refusal("10.255.255.1:9")}`);
    expect((await run(harness, code("http://surogate-test.invalid:9/"))).ok?.output).toBe(
      "403\n\nThis computer could not look up surogate-test.invalid:9.",
    );
    expect(asks(harness)).toHaveLength(1);
  });
});

describe("a chat's network grants, as the app keeps them", { timeout: 60_000 }, () => {
  const ROOT = "77777777-7777-4777-8777-777777777777";
  const OTHER = "88888888-8888-4888-8888-888888888888";
  let journal: OperationJournal;
  let tools: ToolHosts[];

  // The user at the desktop's network prompts: each gets the next of *answers*; past the
  // end, a prompt stays open until it is dismissed.
  class User implements ApprovalPrompts {
    readonly asked: ApprovalRequest[] = [];
    dismissed = 0;
    constructor(readonly answers: ApprovalAnswer[] = []) {}
    approve(request: ApprovalRequest, signal: AbortSignal): Promise<ApprovalAnswer> {
      this.asked.push(request);
      const answer = this.answers.shift();
      if (answer) return Promise.resolve(answer);
      return new Promise((resolve) => signal.addEventListener("abort", () => {
        this.dismissed += 1;
        resolve("allow_session");
      }, { once: true }));
    }
    confirmFreeMode(): Promise<boolean> {
      return Promise.resolve(false);
    }
  }

  // The device as the app wires it, without the link: the approvals decide what its hosts' commands reach.
  function device(user: User): ToolHosts {
    const approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    const made = new ToolHosts({
      bindingOf: (root) => journal.bindings.get(root),
      dataDir: join(base, "data"),
      env: { HOME: join(base, "home"), LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
      network: approvals,
    });
    tools.push(made);
    return made;
  }

  const runIn = (executor: ToolHosts, root: string, command: string, timeout = 20) => {
    const operation: Operation = {
      id: `op-${next++}`, sessionId: root, callingSessionId: root, invocationId: "1:call", ordinal: next, kind: "run",
      args: { command, workdir: null, timeout }, digest: "d",
    };
    return executor.run(operation, new AbortController().signal) as Promise<Answer>;
  };

  beforeEach(() => {
    journal = new OperationJournal(join(base, "journal.sqlite"));
    tools = [];
    for (const [root, name] of [[ROOT, "folder"], [OTHER, "other"]] as const) {
      mkdirSync(join(base, name), { recursive: true });
      const { dev, ino } = statSync(join(base, name));
      journal.bindings.add({ root, nonce: `nonce-${root}`, folder: join(base, name), dev, ino, boot: BOOT_ID, mode: "free", boundAt: 1 });
    }
  });

  afterEach(async () => {
    for (const made of tools) await made.stop();
    journal.close();
  });

  it("keeps a host allowed for the session for the chat's next host and the next launch, and for no other chat", async () => {
    const user = new User(["allow_session", "deny"]);
    const first = device(user);
    expect((await runIn(first, ROOT, code(`http://${AWAY}:9/`))).ok?.output).toMatch(PASSED);
    expect(user.asked).toEqual([{
      kind: "network", chat: { agent: "Research assistant", root: ROOT, calling: ROOT, folder: join(base, "folder") },
      host: AWAY, port: 9, privateNetwork: false,
    }]);
    // The chat's host stops, as it does when the computer's access ends; the next one starts with the grant.
    await first.end();
    expect((await runIn(first, ROOT, code(`http://${AWAY}:7/`))).ok?.output).toMatch(PASSED);
    // Another chat on this computer is asked on its own.
    expect((await runIn(first, OTHER, code(`http://${AWAY}:9/`))).ok?.output).toBe(`403\n\n${refusal(`${AWAY}:9`)}`);
    expect(user.asked.map((request) => request.chat.root)).toEqual([ROOT, OTHER]);
    // The app quits, and starts again.
    await first.stop();
    journal.close();
    journal = new OperationJournal(join(base, "journal.sqlite"));
    const again = new User();
    expect((await runIn(device(again), ROOT, code(`http://${AWAY}:9/`))).ok?.output).toMatch(PASSED);
    expect(again.asked).toEqual([]);
  });

  it("dismisses an open network prompt when the chat's host stops, and keeps nothing from it", async () => {
    const user = new User();
    const executor = device(user);
    const running = runIn(executor, ROOT, `curl -sS -o /dev/null http://${AWAY}:9/`);
    await vi.waitFor(() => expect(user.asked).toHaveLength(1), { timeout: 20_000 });
    await executor.end();
    // The host stopped its command with it.
    expect((await running).error?.type).toBe("cancelled");
    // What the dismissed prompt settled with is not its user's answer.
    expect([user.dismissed, journal.bindings.domains(ROOT)]).toEqual([1, []]);
  });
});
