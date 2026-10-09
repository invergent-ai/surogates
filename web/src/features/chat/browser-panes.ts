// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Each chat's browser pane, kept while the page lives: what it shows beside where the browser is,
// and what it has told the chat's own control route. The pane is drawn anew each time it is opened,
// and what the server was told must outlast each drawing. A sub-agent's view posts to its own
// route, and the server tells the chat it works under: the browser is held for that chat and every
// session under it, and at a hand back it is that chat's agent that goes on.
import { acquireBrowserControl, releaseBrowserControl } from "@/api/sessions";
import { browserPanes } from "@/lib/local-chat";

export const browserPaneOf = browserPanes((sessionId) => ({
  acquire: () => acquireBrowserControl(sessionId),
  release: (handedBack) => releaseBrowserControl(sessionId, handedBack),
}));
