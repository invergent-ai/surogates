// What an agent's web client may do with its window (spec, Section 1): stay on its
// origin, open the one popup the web client needs, and send every other web address
// to the system browser. Claude Desktop keeps its own view the same way (index.js:
// will-navigate, will-redirect, and a window-open handler that denies and opens outside).

import { fileURLToPath } from "node:url";

// The Composio sign-in popup (sdk/agent-chat-react/src/lib/oauth-popup.ts): the page watches it close.
const POPUP = { name: "composio-oauth", origin: "https://connect.composio.dev" };

const PERMISSIONS: ReadonlySet<string> = new Set(["notifications", "clipboard-sanitized-write"]);

const parsed = (url: string): URL | null => {
  try {
    return new URL(url);
  } catch {
    return null;
  }
};

export const sameOrigin = (origin: string, url: string): boolean => parsed(url)?.origin === origin;

// Notifications and clipboard writes, for the agent's own frames only: an embedded frame from elsewhere gets nothing.
export const permitted = (origin: string, permission: string, requestingUrl: string): boolean =>
  PERMISSIONS.has(permission) && sameOrigin(origin, requestingUrl);

// http and https go to the system browser; nothing else leaves the app.
export const external = (url: string): boolean => ["http:", "https:"].includes(parsed(url)?.protocol ?? "");

export function windowOpen(url: string, frameName: string): "popup" | "external" | "deny" {
  if (frameName === POPUP.name && parsed(url)?.origin === POPUP.origin) return "popup";
  return external(url) ? "external" : "deny";
}

// The web client's own pages the sidebar and the user menu open: a path of its own, never one the
// page made up. Settings opens on its Devices too, as the "computer added" notice opens it.
const PATHS = /^\/(?:chat|chats|inbox|missions|skills|settings|settings\?tab=devices)$|^\/chat\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export const webClientPath = (path: string): boolean => PATHS.test(path);

/** A call from *page*, one of the app's own pages, in its top frame: no other page, a file dropped there included. */
export function ownPage(frame: { readonly url: string; readonly parent: unknown } | null, page: string): boolean {
  if (frame == null || frame.parent !== null || !frame.url.startsWith("file:")) return false;
  // A file URL no path can be (another host's, an encoded slash) is none of the app's pages either.
  try {
    return fileURLToPath(frame.url) === page;
  } catch {
    return false;
  }
}
