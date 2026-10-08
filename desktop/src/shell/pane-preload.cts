// The preload of the Overview pane's transcript. It exposes nothing to the page, which has no
// bridge: it only hears the page's keys, in its own world, and leaves them to the page. The
// keyboard leaves the transcript only at its edges, for the pane's head in the window's page:
// Shift+Tab from its first control, or from none, to Open, and Escape to Back, each only where
// the page left the key alone and has no dialog of its own open.

import { ipcRenderer } from "electron";

// What Tab stops at, as a page's own controls are written.
const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex], [contenteditable="true"]';

// The page's first control Tab stops at: enabled, shown, and not taken out of the order.
function first(): Element | null {
  for (const each of document.querySelectorAll<HTMLElement>(FOCUSABLE)) {
    if (each.tabIndex >= 0 && !each.matches(":disabled") && each.getClientRects().length > 0) return each;
  }
  return null;
}

const dialogOpen = () => document.querySelector('[role="dialog"], [role="alertdialog"], dialog[open]') !== null;

// Heard after the page's own handlers: a key the page took, or one heard while a dialog of its own is
// open, stays the page's. Its default, the step Shift+Tab takes, is still to come, and is stopped here.
if (window.top === window) {
  window.addEventListener("keydown", (event) => {
    if (!event.isTrusted || event.defaultPrevented || dialogOpen()) return;
    if (event.key === "Tab" && event.shiftKey) {
      const at = document.activeElement;
      if (at !== null && at !== document.body && at !== first()) return;
      event.preventDefault();
      ipcRenderer.send("pane:leave", "open");
    } else if (event.key === "Escape") {
      ipcRenderer.send("pane:leave", "back");
    }
  });
}
