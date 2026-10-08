import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type Agent, AgentStore, canonicalOrigin, connectAgent, describeAgent, type Get, linkUrl, linksFor, partitionFor, readAgent,
} from "../src/shell/agents.js";

const CONFIG = { agent_id: "agent-1", desktop_sessions: true, multi_session: true, self_registration_enabled: false };
const AGENT: Agent = {
  origin: "https://agent.example.com", agentId: "agent-1", name: "agent.example.com", desktopSessions: true, multiSession: true, consoleUrl: null,
};

// A server as the shell's GET meets it: *hops* are the redirects it sends, each told to the
// caller before it is followed, and a hop the caller refuses ends the request with that refusal.
function answering(body: unknown, hops: string[] = [], status = 200) {
  const asked: string[] = [];
  const get: Get = async (requested, hop) => {
    asked.push(requested);
    for (const to of hops) hop(to);
    // A string is the body itself, as a page that is not JSON serves it.
    return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
  };
  return { asked, get };
}

let dir: string;
let store: AgentStore;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "agents-"));
  store = new AgentStore(join(dir, "agent.json"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("an agent's address", () => {
  it.each([
    ["https://Agent.Example.com/chat/1?x=1#y", "https://agent.example.com"],
    ["agent.example.com", "https://agent.example.com"],
    ["https://agent.example.com:443", "https://agent.example.com"],
    ["https://agent.example.com:8443/", "https://agent.example.com:8443"],
    ["http://localhost:8000", "http://localhost:8000"],
    ["https://surogates.k8s.localhost", "https://surogates.k8s.localhost"],
    ["http://127.0.0.1:5173", "http://127.0.0.1:5173"],
    ["http://[::1]:8000", "http://[::1]:8000"],
  ])("%s is the origin %s", (input, origin) => {
    expect(canonicalOrigin(input)).toBe(origin);
  });

  it.each([
    ["not a url at all", "That is not a web address"],
    ["https://user:pass@agent.example.com", "An address with a user name or password in it is refused"],
    ["http://agent.example.com", "Use an https:// address: http:// is only for this computer's own servers"],
    // The link's resolver may ask the network for a name under .localhost.
    ["http://surogates.k8s.localhost", "Use an https:// address: http:// is only for this computer's own servers"],
    ["ftp://agent.example.com", "Use an https:// address: http:// is only for this computer's own servers"],
  ])("%s is refused", (input, message) => {
    expect(() => canonicalOrigin(input)).toThrow(message);
  });

  it("gives the link's address and a storage partition of its own", () => {
    expect(linkUrl("https://agent.example.com")).toBe("wss://agent.example.com/api/v1/devices/connect");
    expect(linkUrl("http://localhost:8000")).toBe("ws://localhost:8000/api/v1/devices/connect");
    const partition = partitionFor("https://agent.example.com", "agent-1");
    expect(partition).toMatch(/^persist:agent-[0-9a-f]{32}$/);
    expect(partitionFor("https://agent.example.com", "agent-2")).not.toBe(partition);
    expect(partitionFor("https://other.example.com", "agent-1")).not.toBe(partition);
  });
});

describe("connecting to the agent", () => {
  it("starts over when the kept agent cannot be read, which is said", () => {
    writeFileSync(join(dir, "agent.json"), "{ not json");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(store.get()).toBeNull();
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      warn.mockRestore();
    }
  });

  it.each([
    ["no object", "[]"],
    ["text", '"x"'],
    ["no origin", JSON.stringify({ ...AGENT, origin: undefined })],
    ["a flag that is no boolean", JSON.stringify({ ...AGENT, multiSession: "yes" })],
    ["plain http to another computer", JSON.stringify({ ...AGENT, origin: "http://evil.example" })],
    ["an origin with a path", JSON.stringify({ ...AGENT, origin: "https://agent.example.com/chat" })],
    ["no console", JSON.stringify({ ...AGENT, consoleUrl: undefined })],
    ["a null origin", JSON.stringify({ ...AGENT, origin: null })],
    ["a console that is no origin", JSON.stringify({ ...AGENT, consoleUrl: "javascript:alert(1)" })],
    ["a console on this computer, for an agent that is not", JSON.stringify({ ...AGENT, consoleUrl: "http://localhost:5173" })],
  ])("starts over when the kept agent has %s, which is said", (_name, held) => {
    writeFileSync(join(dir, "agent.json"), held);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(store.get()).toBeNull();
      expect(String(warn.mock.calls[0]?.[0])).toMatch(/agent\.json does not hold an agent, so Surogate starts without it/);
    } finally {
      warn.mockRestore();
    }
  });

  it("asks the server who it is, confirms the new origin, and keeps it as the one agent", async () => {
    const { asked, get } = answering(CONFIG);
    const confirmed: Array<[string, string]> = [];
    const agent = await connectAgent("agent.example.com", {
      get, store, confirm: (found, typed) => {
        confirmed.push([found.origin, typed]);
        return Promise.resolve(true);
      },
    });
    expect(asked).toEqual(["https://agent.example.com/api/v1/auth/config"]);
    expect(confirmed).toEqual([["https://agent.example.com", "https://agent.example.com"]]);
    expect(agent).toEqual(AGENT);
    expect(new AgentStore(join(dir, "agent.json")).get()).toEqual(AGENT);
  });

  it("keeps nothing when the user declines", async () => {
    const { get } = answering(CONFIG);
    expect(await connectAgent("agent.example.com", { get, store, confirm: () => Promise.resolve(false) })).toBeNull();
    expect(store.get()).toBeNull();
  });

  it("confirms the origin a redirect ended on, and keeps that one", async () => {
    const { get } = answering(CONFIG, ["https://agent.example.com/v2/auth/config", "https://agents.example.org/api/v1/auth/config"]);
    const confirmed: Array<[string, string]> = [];
    const agent = await connectAgent("agent.example.com", {
      get, store, confirm: (found, typed) => {
        confirmed.push([found.origin, typed]);
        return Promise.resolve(true);
      },
    });
    expect(confirmed).toEqual([["https://agents.example.org", "https://agent.example.com"]]);
    expect(agent?.origin).toBe("https://agents.example.org");
    expect(store.get()?.origin).toBe("https://agents.example.org");
  });

  it("reads an older server's missing desktop_sessions as no local folders, and a single conversation as such", async () => {
    const { get } = answering({ agent_id: "agent-1", multi_session: false });
    const agent = await connectAgent("agent.example.com", { get, store, confirm: () => Promise.resolve(true) });
    expect(agent).toMatchObject({ desktopSessions: false, multiSession: false });
  });

  it.each([
    ["a redirect off the agent's path", answering(CONFIG, ["https://agent.example.com/login"]),
      "agent.example.com sent Surogate to https://agent.example.com/login, which is not an agent"],
    ["a redirect to plain http", answering(CONFIG, ["http://agent.example.com/api/v1/auth/config"]),
      "Use an https:// address: http:// is only for this computer's own servers"],
    // Back on https at the end, but an http hop on the way could have chosen where it ended.
    ["a plain http hop on the way", answering(CONFIG, ["http://evil.example/x", "https://agents.example.org/api/v1/auth/config"]),
      "Use an https:// address: http:// is only for this computer's own servers"],
    ["too many redirects", answering(CONFIG, Array.from({ length: 6 }, (_, n) => `https://agent.example.com/hop/${n}`)),
      "agent.example.com redirects Surogate too many times"],
    ["an error", answering({}, [], 404), "No Surogate agent answers at https://agent.example.com (HTTP 404)"],
    ["no agent id", answering({ firebase: null }), "https://agent.example.com is not a Surogate agent"],
    ["no JSON", answering("<html>"), "https://agent.example.com is not a Surogate agent"],
  ])("refuses %s", async (_name, server, message) => {
    await expect(connectAgent("agent.example.com", { ...server, store, confirm: () => Promise.resolve(true) }))
      .rejects.toThrow(message);
    expect(store.get()).toBeNull();
  });

  it("says when the server cannot be reached", async () => {
    await expect(connectAgent("agent.example.com", {
      get: () => Promise.reject(new TypeError("fetch failed")), store, confirm: () => Promise.resolve(true),
    })).rejects.toThrow("Could not reach https://agent.example.com: fetch failed");
  });

  it("gives up on a server that never answers", async () => {
    const silent: Get = (_url, _hop, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason));
    });
    await expect(readAgent("https://agent.example.com", silent, 50)).rejects.toThrow("Could not reach https://agent.example.com: no answer within 0.05 s");
  });
});

describe("what the shell says about this computer and the agent", () => {
  it.each([
    [{ ...AGENT, desktopSessions: false }, null, "This server doesn't support local folders yet"],
    [{ ...AGENT, multiSession: false }, null, "This agent keeps one conversation, so its chats stay in the cloud"],
    [AGENT, null, "Sign in to this agent to let it work on folders of this computer"],
    [AGENT, { status: "connecting", computer: null }, "Connecting…"],
    [AGENT, { status: "connected", computer: "thinkpad" }, "Connected as thinkpad"],
    [AGENT, { status: "offline", computer: "thinkpad" }, "Offline: reconnecting"],
    [AGENT, { status: "revoked", computer: "thinkpad" }, "Local access revoked"],
    [AGENT, { status: "unauthenticated", computer: null }, "The agent no longer accepts this computer"],
    [AGENT, { status: "superseded", computer: null }, "Another copy of Surogate took over this computer's connection"],
    [AGENT, { status: "update_required", computer: null }, "Update Surogate: this version is too old for the agent"],
    [AGENT, { status: "stopped", computer: null }, "Stopped"],
  ] as const)("%o with %o: %s", (agent, device, text) => {
    expect(describeAgent(agent, device)).toBe(text);
  });
});
describe("the console an agent's user is sent to", () => {
  it.each([
    ["https://ops.surogate.ai", "https://ops.surogate.ai"],
    ["https://Ops.Acme.local/", "https://ops.acme.local"],
    ["https://ops.acme.local:8443/console", "https://ops.acme.local:8443"],
    ["http://localhost:5173", null],
    [undefined, null],
    [null, null],
    ["", null],
    [42, null],
    ["http://ops.acme.com", null],
    ["javascript:alert(1)", null],
    ["file:///etc/passwd", null],
    ["https://user:secret@ops.acme.com", null],
  ])("is %s's origin, %s", async (named, console) => {
    const { get } = answering({ ...CONFIG, console_url: named });
    expect((await readAgent("https://agent.example.com", get)).consoleUrl).toBe(console);
  });

  it("is plain http on this computer only for an agent on this computer too, as its development servers are", async () => {
    const { get } = answering({ ...CONFIG, console_url: "http://localhost:5173/work" });
    expect((await readAgent("http://127.0.0.1:8000", get)).consoleUrl).toBe("http://localhost:5173");
  });
});
describe("the links the user menu and Settings open", () => {
  it("lead into the console's settings where the agent names its console", () => {
    expect(linksFor({ ...AGENT, consoleUrl: "https://ops.acme.local" })).toEqual({
      help: "https://docs.surogate.ai/work/",
      usage: "https://ops.acme.local/work/settings/usage",
      billing: "https://ops.acme.local/work/settings/billing",
    });
  });

  it("are help alone where the agent names no console, surogate.ai's own included, and before there is an agent", () => {
    expect(linksFor(AGENT)).toEqual({ help: "https://docs.surogate.ai/work/" });
    expect(linksFor({ ...AGENT, origin: "https://acme.surogate.ai" })).toEqual({ help: "https://docs.surogate.ai/work/" });
    expect(linksFor(null)).toEqual({ help: "https://docs.surogate.ai/work/" });
  });
});
