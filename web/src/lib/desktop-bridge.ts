// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Surogate Desktop's bridge, as the web client uses it (desktop design, Section 8).
// In a browser getDesktop() is undefined, and nothing here runs.

import type { DesktopAccount, DesktopBridge } from "./desktop-bridge-contract";
import type { Project, ProjectsSource, ThreadRow } from "./projects";

export type * from "./desktop-bridge-contract";

export function getDesktop(): DesktopBridge | undefined {
  return typeof window === "undefined" ? undefined : window.surogateDesktop;
}

// A session, as GET /api/v1/sessions?include_descendants=true lists it: every root, with every session under it.
export interface SessionRow {
  id: string;
  parentId: string | null;
  channel: string;
  title: string | null;
  status: string;
  createdAt: string;
  updatedAt: string;
}

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const latestFirst = (a: { updatedAt: string }, b: { updatedAt: string }) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt);
// The server's session times are naive UTC: the wire sends every time with its Z (Section 12).
const utc = (time: string): string => new Date(/(Z|[+-]\d\d:?\d\d)$/i.test(time) ? time : `${time}Z`).toISOString();

// A thread's group, by Section 12's rules as far as a session alone tells them: a pending
// question or approval, a computer away and a resolved mark wait for the server's own projects.
function threadOf(session: SessionRow, now: number): ThreadRow {
  const updatedAt = utc(session.updatedAt);
  const [group, reason, statusLine]: [ThreadRow["group"], ThreadRow["reason"], string | null] =
    session.status === "failed" ? ["waiting", "failed", "Failed"]
    : session.status === "active" ? ["working", null, null]
    : [now - Date.parse(updatedAt) > WEEK_MS ? "resolved" : "idle", null, session.status === "paused" ? "Paused" : null];
  return {
    id: session.id, title: session.title ?? "Untitled thread", group, reason, statusLine, progress: null, files: [],
    place: { kind: "cloud" }, createdAt: utc(session.createdAt), updatedAt, resolvedAt: null,
  };
}

// Each root web session is a project and its master; every session under it, whichever its channel, is one of its threads.
function projectsOf(sessions: SessionRow[], now: number): Array<{ project: Project; threads: ThreadRow[] }> {
  const children = new Map<string, SessionRow[]>();
  for (const session of sessions) {
    if (session.parentId !== null) children.set(session.parentId, [...(children.get(session.parentId) ?? []), session]);
  }
  const under = (id: string): SessionRow[] => (children.get(id) ?? []).flatMap((child) => [child, ...under(child.id)]);
  return sessions.filter((session) => session.parentId === null && session.channel === "web").map((root) => {
    const threads = under(root.id).map((session) => threadOf(session, now)).sort(latestFirst);
    const project: Project = {
      id: root.id,
      name: root.title ?? "Untitled project",
      icon: null,
      createdAt: utc(root.createdAt),
      updatedAt: [{ updatedAt: utc(root.updatedAt) }, ...threads].sort(latestFirst)[0]!.updatedAt,
      waiting: threads.filter((thread) => thread.group === "waiting").length,
      working: threads.filter((thread) => thread.group === "working").length,
      goal: null,
      instructions: "",
      masterSessionId: root.id,
      coordinatorTier: null,
      threadTier: null,
    };
    return { project, threads };
  }).sort((a, b) => latestFirst(a.project, b.project));
}

/**
 * The projects as the current server can give them, until it keeps projects of its own
 * (/v1/workstreams): from the user's sessions. It changes nothing, and tells no change: the
 * shell asks again when its window comes to the front.
 */
export function sessionProjects(api: Pick<JoinApi, "sessions">, now: () => number = Date.now): ProjectsSource {
  const read = async () => projectsOf(await api.sessions(), now());
  const one = async (projectId: string) => {
    const found = (await read()).find(({ project }) => project.id === projectId);
    if (!found) throw new Error("No such project");
    return found;
  };
  const unchanged = () => Promise.reject(new Error("This agent's server keeps no projects yet"));
  return {
    list: async () => (await read()).map(({ project: { id, name, icon, createdAt, updatedAt, waiting, working } }) =>
      ({ id, name, icon, createdAt, updatedAt, waiting, working })),
    get: async (projectId) => (await one(projectId)).project,
    threads: async (projectId) => (await one(projectId)).threads,
    library: async () => [],
    routines: async () => [],
    create: unchanged,
    update: unchanged,
    archive: unchanged,
    resolve: unchanged,
    reopen: unchanged,
    subscribe: () => () => {},
  };
}

// What joining needs of the agent's API, as the signed-in user.
export interface JoinApi {
  register(name: string): Promise<{ token: string }>; // POST /api/v1/devices
  account(): Promise<DesktopAccount>; // GET /api/v1/auth/me
  sessions(): Promise<SessionRow[]>; // GET /api/v1/sessions?include_descendants=true
}

// One registration per desktop and page load: a remount must not register a second device.
const registrations = new WeakMap<DesktopBridge, Promise<void>>();

function register(desktop: DesktopBridge, api: JoinApi): Promise<void> {
  let registering = registrations.get(desktop);
  if (!registering) {
    registering = (async () => {
      const { device, computerName, localFolders } = await desktop.getDevice();
      if (device || !localFolders) return;
      await desktop.registerDevice((await api.register(computerName)).token);
    })();
    registrations.set(desktop, registering);
  }
  return registering;
}

/**
 * Once the user is signed in: tell the desktop who it is, serve it their projects, and register
 * this computer with the agent if it is not yet. The returned function withdraws the account
 * and the projects, as when the user signs out.
 */
export function joinDesktop(desktop: DesktopBridge, api: JoinApi): () => void {
  let stopped = false;
  // Each step waits on the desktop or the server: a sign-out meanwhile ends the join there.
  void (async () => {
    const account = await api.account();
    if (stopped) return;
    await desktop.setAccount(account);
    if (stopped) return;
    await desktop.registerProjects(sessionProjects(api));
    if (stopped) return;
    await register(desktop, api);
  })().catch((error: unknown) => console.warn("Surogate Desktop could not join this account", error));
  return () => {
    stopped = true;
    leaveDesktop(desktop);
  };
}

/** Nobody is signed in on this page, as on the sign-in page an expired session ends on: the desktop forgets the account and its projects. */
export function leaveDesktop(desktop: DesktopBridge): void {
  void desktop.setAccount(null);
  void desktop.registerProjects(null);
}
