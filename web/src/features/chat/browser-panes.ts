// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Each chat's browser pane, kept while the page lives: what it shows beside where the browser is,
// and what it has told the chat's own control route. The pane is drawn anew each time it is opened,
// and what the server was told must outlast each drawing. A sub-agent's chat is told as itself: its
// own transcript says a take-over and a hand back, and its own agent is woken.
import { acquireBrowserControl, releaseBrowserControl } from "@/api/sessions";
import { browserPanes } from "@/lib/local-chat";

export const browserPaneOf = browserPanes((sessionId) => ({
  acquire: () => acquireBrowserControl(sessionId),
  release: () => releaseBrowserControl(sessionId),
}));
