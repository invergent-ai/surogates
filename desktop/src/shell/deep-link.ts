// A surogate:// link (spec, Section 7), as the system hands one to the app: on Linux, in the
// arguments it starts with, or in a second launch's, which the running app's second-instance
// hands to its link handler. Only surogate://open?url=<agent address> is one: it opens that
// agent, at a page of its web client when the address names one. A link names no local path,
// and grants nothing.

import { notBuilt } from "../platform.js";
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
export const linkOfLaunch: (argv: readonly string[]) => OpenLink | null = process.platform === "linux" ? linkIn : () => notBuilt("The arrival of surogate:// links");
