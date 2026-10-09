// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The browser pane of a chat whose browser is on the user's computer (Section 5): where it is, who
// holds it, and, in Surogate Desktop on that computer, Show browser and Take over or Hand back, or
// Settings → Browser where it has none. The window on that computer is the live view.
import { useEffect, useRef, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { type BrowserAction, computerBrowser } from "@/lib/local-chat";

import { browserPaneOf } from "./browser-panes";
import { useLocalChat } from "./local-chat-bar";

const LABELS: Record<BrowserAction, string> = {
  show: "Show browser",
  takeOver: "Take over",
  handBack: "Hand back",
  settings: "Open Settings → Browser",
};

export function ComputerBrowser({
  sessionId,
  available,
  readOnly,
}: { sessionId: string; available: boolean; readOnly: boolean }) {
  const { chat, desktop, reread } = useLocalChat(sessionId);
  const pane = browserPaneOf(sessionId);
  const { asking, failure, said, answers } = useSyncExternalStore(
    pane.subscribe,
    pane.state,
  );
  // Who holds the browser is the computer's to say: asked again once the desktop has answered a
  // take-over or a hand back, whatever it answered. A hand back that handed nothing back may mean
  // the browser was taken over from another chat meanwhile.
  const heard = useRef(answers);
  useEffect(() => {
    if (answers !== heard.current) {
      heard.current = answers;
      reread();
    }
  }, [answers, reread]);
  if (!chat) {
    return null;
  }
  const { text, actions } = computerBrowser(
    chat,
    { available, readOnly },
    desktop,
    asking,
  );
  return (
    <div
      data-testid="computer-browser"
      className="flex max-w-sm flex-col items-center gap-3"
    >
      <p>{text}</p>
      {actions.length > 0 && (
        <div className="flex flex-wrap justify-center gap-2">
          {actions.map((action) => (
            <Button
              key={action}
              variant="outline"
              size="sm"
              // The desktop's call is the first thing its press does, and is made once: the desktop
              // lets one call through for a click of its user's, and asks one of a hand back.
              onClick={() => {
                if (desktop) {
                  pane.press(action, chat.root, desktop);
                }
              }}
            >
              {LABELS[action]}
            </Button>
          ))}
        </div>
      )}
      {/* Whether the agent goes on once the browser is handed back, as the server answered. */}
      <output>{said}</output>
      <span role="alert" className="text-destructive">
        {failure}
      </span>
    </div>
  );
}
