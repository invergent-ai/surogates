import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type Agent, AgentStore, canonicalOrigin, connectAgent, consoleFor, describeAgent, linkUrl, linksFor, partitionFor,
} from "../src/shell/agents.js";

const CONFIG = { agent_id: "agent-1", desktop_sessions: true, multi_session: true, self_registration_enabled: false };
const AGENT: Agent = {
  origin: "https://agent.example.com", agentId: "agent-1", name: "agent.example.com", desktopSessions: true, multiSession: true,
};

// A server as fetch meets it: *url* is where the request ended, after any redirect.
function answering(body: unknown, url = "https://agent.example.com/api/v1/auth/config", status = 200) {
  const asked: string[] = [];
  const fetch = (requested: string) => {
    asked.push(requested);
    // A string is the body itself, as a page that is not JSON serves it.
    const response = new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
    Object.defineProperty(response, "url", { value: url });
    return Promise.resolve(response);
  };
  return { asked, fetch };
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
    ["http://surogates.k8s.localhost", "http://surogates.k8s.localhost"],
    ["http://127.0.0.1:5173", "http://127.0.0.1:5173"],
    ["http://[::1]:8000", "http://[::1]:8000"],
  ])("%s is the origin %s", (input, origin) => {
    expect(canonicalOrigin(input)).toBe(origin);
  });

  it.each([
    ["not a url at all", "That is not a web address"],
    ["https://user:pass@agent.example.com", "An address with a user name or password in it is refused"],
    ["http://agent.example.com", "Use an https:// address: http:// is only for this computer's own servers"],
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
    const { asked, fetch } = answering(CONFIG);
    const confirmed: Array<[string, string]> = [];
    const agent = await connectAgent("agent.example.com", {
      fetch, store, confirm: (found, typed) => {
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
    const { fetch } = answering(CONFIG);
    expect(await connectAgent("agent.example.com", { fetch, store, confirm: () => Promise.resolve(false) })).toBeNull();
    expect(store.get()).toBeNull();
  });

  it("confirms the origin a redirect ended on, and keeps that one", async () => {
    const { fetch } = answering(CONFIG, "https://agents.example.org/api/v1/auth/config");
    const confirmed: Array<[string, string]> = [];
    const agent = await connectAgent("agent.example.com", {
      fetch, store, confirm: (found, typed) => {
        confirmed.push([found.origin, typed]);
        return Promise.resolve(true);
      },
    });
    expect(confirmed).toEqual([["https://agents.example.org", "https://agent.example.com"]]);
    expect(agent?.origin).toBe("https://agents.example.org");
    expect(store.get()?.origin).toBe("https://agents.example.org");
  });

  it("reads an older server's missing desktop_sessions as no local folders, and a single conversation as such", async () => {
    const { fetch } = answering({ agent_id: "agent-1", multi_session: false });
    const agent = await connectAgent("agent.example.com", { fetch, store, confirm: () => Promise.resolve(true) });
    expect(agent).toMatchObject({ desktopSessions: false, multiSession: false });
  });

  it.each([
    ["a redirect off the agent's path", answering(CONFIG, "https://agent.example.com/login"),
      "agent.example.com sent Surogate to https://agent.example.com/login, which is not an agent"],
    ["a redirect to plain http", answering(CONFIG, "http://agent.example.com/api/v1/auth/config"),
      "Use an https:// address: http:// is only for this computer's own servers"],
    ["an error", answering({}, undefined, 404), "No Surogate agent answers at https://agent.example.com (HTTP 404)"],
    ["no agent id", answering({ firebase: null }), "https://agent.example.com is not a Surogate agent"],
    ["no JSON", answering("<html>"), "https://agent.example.com is not a Surogate agent"],
  ])("refuses %s", async (_name, server, message) => {
    await expect(connectAgent("agent.example.com", { ...server, store, confirm: () => Promise.resolve(true) }))
      .rejects.toThrow(message);
    expect(store.get()).toBeNull();
  });

  it("says when the server cannot be reached", async () => {
    await expect(connectAgent("agent.example.com", {
      fetch: () => Promise.reject(new TypeError("fetch failed")), store, confirm: () => Promise.resolve(true),
    })).rejects.toThrow("Could not reach https://agent.example.com: fetch failed");
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
    ["https://acme.surogate.ai", "https://ops.surogate.ai"],
    ["https://surogate.ai", null],
    ["https://agent.example.com", null],
    ["https://agents.acme.local", null],
    ["https://evilsurogate.ai", null],
  ])("for %s is %s", (origin, console) => {
    expect(consoleFor(origin)).toBe(console);
  });
});
describe("the links the user menu and Settings open", () => {
  it("lead into the console's settings for an agent surogate.ai hosts", () => {
    expect(linksFor("https://acme.surogate.ai")).toEqual({
      help: "https://docs.surogate.ai/work/",
      usage: "https://ops.surogate.ai/work/settings/usage",
      billing: "https://ops.surogate.ai/work/settings/billing",
    });
  });

  it("are help alone for an install of its own, and before there is an agent", () => {
    expect(linksFor("https://agent.example.com")).toEqual({ help: "https://docs.surogate.ai/work/" });
    expect(linksFor(null)).toEqual({ help: "https://docs.surogate.ai/work/" });
  });
});
