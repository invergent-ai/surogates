// A surogate:// link (spec, Section 7), as the system hands one to the app: in the arguments
// it starts with, or in a second launch's, which the running app is given, as Claude Desktop's
// second-instance hands its argv to its link handler (index.js). Only
// surogate://open?url=<agent address> is one: it opens that agent, at a page of its web client
// when the address names one. A link names no local path, and grants nothing.

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
