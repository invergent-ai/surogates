// The one agent the app signs in to (Progress, the correction of 2026-10-06): the
// computer joins that agent, and the shell shows its projects. An agent is its
// server's canonical origin: the app asks that origin's /auth/config who it is, and
// the origin, the one a redirect ended on included, is kept only once the user
// confirms it natively (spec, Section 7).

import { createHash } from "node:crypto";
import type { LinkStatus } from "../link/client.js";
import { readState, writeState } from "./state-file.js";

const CONFIG_PATH = "/api/v1/auth/config";

export interface Agent {
  origin: string;
  agentId: string;
  name: string; // the server names no agent: its host does
  desktopSessions: boolean; // the server can bind a chat to a folder of this computer
  multiSession: boolean; // false: one conversation, which stays in the cloud
}

// Plain http only for this computer's own servers, as in development.
const loopback = (host: string): boolean =>
  host === "localhost" || host.endsWith(".localhost") || host === "[::1]" || /^127(\.\d{1,3}){3}$/.test(host);

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

// An agent as connectAgent keeps one: its origin canonical, so the bridge's exact-origin check holds.
function isAgent(value: unknown): value is Agent {
  const { origin, agentId, name, desktopSessions, multiSession } =
    (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  if (typeof origin !== "string" || typeof agentId !== "string" || agentId === "" || typeof name !== "string") return false;
  if (typeof desktopSessions !== "boolean" || typeof multiSession !== "boolean") return false;
  try {
    return canonicalOrigin(origin) === origin;
  } catch {
    return false;
  }
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
}

export interface ConnectOptions {
  fetch(url: string): Promise<Response>;
  store: AgentStore;
  // The native confirmation of the origin: *typed* is where the user pointed, agent.origin where it ended.
  confirm(agent: Agent, typed: string): Promise<boolean>;
}

/** The agent *input* names, kept once the user confirmed it; null when they declined. */
export async function connectAgent(input: string, options: ConnectOptions): Promise<Agent | null> {
  const typed = canonicalOrigin(input);
  const agent = await askAgent(typed, options.fetch);
  if (!(await options.confirm(agent, typed))) return null;
  options.store.set(agent);
  return agent;
}

async function askAgent(origin: string, fetch: ConnectOptions["fetch"]): Promise<Agent> {
  const asked = `${origin}${CONFIG_PATH}`;
  let response: Response;
  try {
    response = await fetch(asked);
  } catch (error) {
    throw new Error(`Could not reach ${origin}: ${error instanceof Error ? error.message : String(error)}`);
  }
  const ended = new URL(response.url || asked);
  if (ended.pathname !== CONFIG_PATH || ended.search) {
    throw new Error(`${new URL(origin).host} sent Surogate to ${ended.href}, which is not an agent`);
  }
  const final = canonicalOrigin(ended.href);
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

// Where an agent's user finds usage, billing and API keys: the Surogate console, for an agent
// surogate.ai hosts. An install of its own has no console the app knows, and its menu shows no link.
export const consoleFor = (origin: string): string | null =>
  new URL(origin).hostname.endsWith(".surogate.ai") ? "https://ops.surogate.ai" : null;
