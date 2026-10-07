// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Surogate Desktop's bridge, as the web client uses it (desktop design, Section 8).
// In a browser getDesktop() is undefined, and nothing here runs.

import type { DesktopAccount, DesktopBridge } from "./desktop-bridge-contract";
import type { ProjectsSource } from "./projects";

export type * from "./desktop-bridge-contract";

export function getDesktop(): DesktopBridge | undefined {
  return typeof window === "undefined" ? undefined : window.surogateDesktop;
}

// What joining needs of the agent's API, as the signed-in user.
export interface JoinApi {
  account(): Promise<DesktopAccount>; // GET /api/v1/auth/me
  projects: ProjectsSource; // the project routes, /api/v1/workstreams
}

/**
 * Once the user is signed in: tell the desktop who it is, and serve it their projects. The app
 * adds this computer to the agent itself, with its own sign-in. The returned function ends the
 * join and withdraws nothing: a join made again at once, as a development build's StrictMode
 * makes one, serves the same account, and a page with no sign-in tells the desktop itself
 * (leaveDesktop).
 */
export function joinDesktop(desktop: DesktopBridge, api: JoinApi): () => void {
  let stopped = false;
  // Each step waits on the desktop or the server: a join ended meanwhile (an unmount, or StrictMode's run again) stops there.
  void (async () => {
    const account = await api.account();
    if (stopped) return;
    await desktop.setAccount(account);
    if (stopped) return;
    await desktop.registerProjects(api.projects);
  })().catch((error: unknown) => console.warn("Surogate Desktop could not join this account", error));
  return () => {
    stopped = true;
  };
}

/** Nobody is signed in on this page, as on the sign-in page an expired session ends on: the desktop forgets the account and its projects. */
export function leaveDesktop(desktop: DesktopBridge): void {
  void desktop.setAccount(null);
  void desktop.registerProjects(null);
}

/**
 * Sign this page in with the app's own sign-in: a one-time code from the desktop, exchanged
 * through *exchange* for the page's own session, which *store* keeps. False while nobody is
 * signed in to the app: the app's window then asks them to sign in.
 */
export async function signInFromDesktop(
  desktop: DesktopBridge,
  exchange: (code: string) => Promise<{ access_token: string; refresh_token: string }>,
  store: (accessToken: string, refreshToken: string) => void,
): Promise<boolean> {
  const issued = await desktop.webSignIn();
  if (!issued) return false;
  const tokens = await exchange(issued.code);
  store(tokens.access_token, tokens.refresh_token);
  return true;
}
