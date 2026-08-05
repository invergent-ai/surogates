// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//

// The phone header is the only chrome that names the current page, and no
// page was filling it — it rendered blank on every route while the page below
// spelled its own title out. Pages now drop that title and the header carries
// it, so every route needs an entry here.
//
// Longest prefix wins, so a nested route resolves to its own name rather than
// its parent's. Order within the list does not matter.
const SECTIONS: readonly (readonly [string, string])[] = [
  ["/chat", "Chat"],
  ["/inbox", "Inbox"],
  ["/missions", "Missions"],
  ["/skills", "Skills"],
  ["/agents", "Agents"],
  ["/integrations", "Integrations"],
  ["/link", "Link a channel"],
  ["/settings", "Settings"],
  ["/coding-agents", "Coding agents"],
];

/**
 * Title for the phone header. Falls back to "Chat", the app's home, for a
 * route with no section of its own.
 */
export function getAppRouteTitle(pathname: string): string {
  let best: string | null = null;
  let bestLength = 0;
  for (const [prefix, label] of SECTIONS) {
    if (pathname.startsWith(prefix) && prefix.length > bestLength) {
      best = label;
      bestLength = prefix.length;
    }
  }
  return best ?? "Chat";
}
