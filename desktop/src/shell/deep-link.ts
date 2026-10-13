// A surogate:// link (spec, Section 7), as the system hands one to the app: on Linux and Windows, in
// the arguments it starts with, or in a second launch's, which the running app is given by Electron's
// second-instance with its argv; on macOS, in an event of
// the running app's, which Launch Services sends it, starting it first where it is not running. Only
// surogate://open?url=<agent address> is one: it opens that agent, at a page of its web client
// when the address names one. A link names no local path, and grants nothing.

import { forThisOs, notBuilt } from "../platform.js";
import { canonicalOrigin } from "./agents.js";
import { webClientPath } from "./window-policy.js";

export interface OpenLink {
  origin: string; // the agent's, canonical
  path: string; // a page of its web client, or "/"
}

// As long as an address the first run takes.
const LONGEST = 2048;

/** The link among *argv*, its first argument of the scheme: null when there is none, or it is no link the app opens. */
export function linkIn(argv: readonly string[]): OpenLink | null {
  const link = argv.find((arg) => arg.toLowerCase().startsWith("surogate:"));
  if (link === undefined || link.length > LONGEST) return null;
  try {
    const url = new URL(link);
    if (url.host.toLowerCase() !== "open" || !["", "/"].includes(url.pathname)) return null;
    const named = url.searchParams.get("url");
    if (named === null) return null;
    const origin = canonicalOrigin(named);
    const { pathname } = new URL(named.includes("://") ? named : `https://${named}`);
    return { origin, path: webClientPath(pathname) ? pathname : "/" };
  } catch {
    return null;
  }
}

/** The link a launch with *argv* hands the app, the first launch or a second, by this OS's way of handing one. */
export const linkOfLaunch: (argv: readonly string[]) => OpenLink | null = forThisOs<(argv: readonly string[]) => OpenLink | null>({
  linux: linkIn,
  // macOS hands no link in a launch's arguments: each comes as an event (listenForLinks).
  darwin: () => null,
  // Windows starts the command the scheme's registration names, the link after it: the MSI registers it machine-wide,
  // and test/win32/dev-app.ps1 for a development build's user (Microsoft, "Registering an Application to a URI Scheme").
  win32: linkIn,
}) ?? (() => notBuilt("The arrival of surogate:// links"));

/** What of Electron's app a link comes by on macOS: its open-url event. */
export interface LinkEvents {
  on(event: "open-url", listener: (event: { preventDefault(): void }, url: string) => void): unknown;
}

// macOS's: each surogate:// address the app is asked to open, the app's own to open. Electron emits it
// for Launch Services' Get URL Apple event (Apple, NSAppleEventManager, kInternetEventClass and
// kAEGetURL), from before the app is ready on, once it has a listener (Electron, app's open-url).
function openUrls(app: LinkEvents, open: (link: OpenLink) => void): void {
  app.on("open-url", (event, url) => {
    event.preventDefault();
    const link = linkIn([url]);
    if (link) open(link);
  });
}

/**
 * Each link the system hands the running app as an event, from now on, given to *open*: on macOS, by
 * *app*'s open-url, a listener set before the app is ready; Linux and Windows hand none so (linkOfLaunch).
 */
export const listenForLinks: (app: LinkEvents, open: (link: OpenLink) => void) => void = forThisOs<(app: LinkEvents, open: (link: OpenLink) => void) => void>({
  linux: () => {}, darwin: openUrls, win32: () => {},
}) ?? (() => notBuilt("The arrival of surogate:// links"));
