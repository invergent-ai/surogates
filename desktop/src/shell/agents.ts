// The one agent the app signs in to (Progress, the correction of 2026-10-06): the
// computer joins that agent, and the shell shows its projects. An agent is its
// server's canonical origin: the app asks that origin's /auth/config who it is, and
// the origin, the one a redirect ended on included, is kept only once the user
// confirms it natively (spec, Section 7).

import { createHash } from "node:crypto";
import { rmSync } from "node:fs";
import type { LinkStatus } from "../link/client.js";
import { readState, writeState } from "./state-file.js";

const CONFIG_PATH = "/api/v1/auth/config";
// How long the agent has to answer, and how many redirects it may send Surogate through.
export const CONFIG_TIMEOUT_MS = 10_000;
const MAX_HOPS = 5;

export interface Agent {
  origin: string;
  agentId: string;
  name: string; // the server names no agent: its host does
  desktopSessions: boolean; // the server can bind a chat to a folder of this computer
  multiSession: boolean; // false: one conversation, which stays in the cloud
  consoleUrl: string | null; // the console the agent names for its users' usage and billing: an origin
}

// Plain http only for this computer's own servers, as in development: localhost and loopback
// addresses. A name under .localhost is not trusted, since the link's resolver may ask the network for it.
const loopback = (host: string): boolean => host === "localhost" || host === "[::1]" || /^127(\.\d{1,3}){3}$/.test(host);

/** The origin *input* names; a bare host is taken as https. Throws, in words for the user, on anything else. */
export function canonicalOrigin(input: string): string {
  let url: URL;
  try {
    url = new URL(input.includes("://") ? input : `https://${input}`);
  } catch {
    throw new Error("That is not a web address");
  }
  if (url.username || url.password) throw new Error("An address with a user name or password in it is refused");
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback(url.hostname))) {
    throw new Error("Use an https:// address: http:// is only for this computer's own servers");
  }
  return url.origin;
}

export const linkUrl = (origin: string): string => `${origin.replace(/^http/, "ws")}/api/v1/devices/connect`;

// One storage partition per agent identity, so one agent's login never reaches another's window.
export const partitionFor = (origin: string, agentId: string): string =>
  `persist:agent-${createHash("sha256").update(`${origin}\n${agentId}`).digest("hex").slice(0, 32)}`;

// *value* as an origin of its own, canonical; null for anything else.
function originOf(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    return canonicalOrigin(value);
  } catch {
    return null;
  }
}

// The console an agent at *origin* names in *value*, as the links open it: an origin of its own, and plain
// http only where the agent is on this computer too, as its development servers are; null for anything else,
// a path or a bare host included: a bare host is the user's to type, not the agent's to serve.
function consoleOf(value: unknown, origin: string): string | null {
  const console = typeof value === "string" && value.includes("://") ? originOf(value) : null;
  return console?.startsWith("http:") && !loopback(new URL(origin).hostname) ? null : console;
}

// An agent as connectAgent keeps one: its origin canonical, so the bridge's exact-origin check holds, and
// its console one too, as the links open it.
function isAgent(value: unknown): value is Agent {
  const { origin, agentId, name, desktopSessions, multiSession, consoleUrl } =
    (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  if (typeof origin !== "string" || typeof agentId !== "string" || agentId === "" || typeof name !== "string") return false;
  if (typeof desktopSessions !== "boolean" || typeof multiSession !== "boolean") return false;
  return originOf(origin) === origin && (consoleUrl === null || consoleOf(consoleUrl, origin) === consoleUrl);
}

export class AgentStore {
  constructor(private readonly path: string) {}

  // Anything else the file holds is said, and the app starts over at its first run.
  get(): Agent | null {
    const kept = readState<unknown>(this.path, null);
    if (kept === null || isAgent(kept)) return kept;
    console.warn(new Error(`${this.path} does not hold an agent, so Surogate starts without it`));
    return null;
  }

  set(agent: Agent): void {
    writeState(this.path, agent);
  }

  // The agent is removed: the app starts over at its first run.
  clear(): void {
    rmSync(this.path, { force: true });
  }
}

/**
 * GET *url*, telling *hop* where each redirect goes before it is followed: a hop it throws on is
 * not followed, and the request rejects with that error. *signal* gives up on it.
 */
export type Get = (url: string, hop: (to: string) => void, signal: AbortSignal) => Promise<Response>;

export interface ConnectOptions {
  get: Get;
  store: AgentStore;
  // The native confirmation of the origin: *typed* is where the user pointed, agent.origin where it ended.
  confirm(agent: Agent, typed: string): Promise<boolean>;
}

/** The agent *input* names, kept once the user confirmed it; null when they declined. */
export async function connectAgent(input: string, options: ConnectOptions): Promise<Agent | null> {
  const typed = canonicalOrigin(input);
  const agent = await readAgent(typed, options.get);
  if (!(await options.confirm(agent, typed))) return null;
  options.store.set(agent);
  return agent;
}

/** Who answers at *origin*: the agent its /auth/config names, at the origin its redirects end on, each hop checked. */
export async function readAgent(origin: string, get: Get, timeoutMs = CONFIG_TIMEOUT_MS): Promise<Agent> {
  const asked = `${origin}${CONFIG_PATH}`;
  let ended = asked;
  let hops = 0;
  const refusal: { error?: Error } = {};
  const hop = (to: string): void => {
    try {
      hops += 1;
      if (hops > MAX_HOPS) throw new Error(`${new URL(origin).host} redirects Surogate too many times`);
      // Every hop is https, or plain http on this computer: an http hop on the way could pick the agent.
      canonicalOrigin(to);
      ended = to;
    } catch (error) {
      refusal.error = error instanceof Error ? error : new Error(String(error));
      throw refusal.error;
    }
  };
  let response: Response;
  try {
    response = await get(asked, hop, AbortSignal.timeout(timeoutMs));
  } catch (error) {
    if (refusal.error) throw refusal.error;
    const why = error instanceof Error && error.name === "TimeoutError" ? `no answer within ${timeoutMs / 1000} s`
      : error instanceof Error ? error.message : String(error);
    throw new Error(`Could not reach ${origin}: ${why}`);
  }
  const end = new URL(ended);
  if (end.pathname !== CONFIG_PATH || end.search) {
    throw new Error(`${new URL(origin).host} sent Surogate to ${end.href}, which is not an agent`);
  }
  const final = canonicalOrigin(end.href);
  if (!response.ok) throw new Error(`No Surogate agent answers at ${final} (HTTP ${response.status})`);
  const config = (await response.json().catch(() => null)) as Record<string, unknown> | null;
  const agentId = typeof config === "object" && config !== null ? config.agent_id : undefined;
  if (typeof agentId !== "string" || agentId === "") throw new Error(`${final} is not a Surogate agent`);
  return {
    origin: final,
    agentId,
    name: new URL(final).host,
    // An older server says nothing, and has no local folders.
    desktopSessions: config?.desktop_sessions === true,
    multiSession: config?.multi_session !== false,
    // Opened in the system browser, so only an https origin, or plain http on this computer for an agent
    // on it too: never a path, a file or a script. An older server, or one whose operator named none, has
    // no console.
    consoleUrl: consoleOf(config?.console_url, final),
  };
}

/** What the shell says about this computer and *agent*; *device* is null until the computer is registered with it. */
export function describeAgent(agent: Agent, device: { status: LinkStatus; computer: string | null } | null): string {
  if (!agent.desktopSessions) return "This server doesn't support local folders yet";
  if (!agent.multiSession) return "This agent keeps one conversation, so its chats stay in the cloud";
  if (device === null) return "Sign in to this agent to let it work on folders of this computer";
  switch (device.status) {
    case "connecting":
      return "Connecting…";
    case "connected":
      return `Connected as ${device.computer ?? "this computer"}`;
    case "offline":
      return "Offline: reconnecting";
    case "revoked":
      return "Local access revoked";
    case "unauthenticated":
      return "The agent no longer accepts this computer";
    case "superseded":
      return "Another copy of Surogate took over this computer's connection";
    case "update_required":
      return "Update Surogate: this version is too old for the agent";
    case "stopped":
      return "Stopped";
  }
}

/**
 * The links the user menu and Settings open for *agent*: only these, never a page's own address. Usage
 * and billing are in the console the agent names; an agent that names none shows neither.
 */
export function linksFor(agent: Agent | null): Record<string, string> {
  const console = agent?.consoleUrl ?? null;
  return {
    help: "https://docs.surogate.ai/work/",
    ...(console ? { usage: `${console}/work/settings/usage`, billing: `${console}/work/settings/billing` } : {}),
  };
}
