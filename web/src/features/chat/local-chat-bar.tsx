// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Over a chat on a folder of the user's computer: the folder, the computer, and, in Surogate
// Desktop on that computer, Show folder and the chat's mode (Section 8). In a browser it names
// the folder and the computer only: the mode is the computer's to keep.
import { FolderIcon, LaptopIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { type Device, listDevices } from "@/api/devices";
import { getSession } from "@/api/sessions";
import { Button } from "@/components/ui/button";
import { type DesktopBinding, getDesktop } from "@/lib/desktop-bridge";
import {
  type FolderCalls,
  type LocalChat,
  folderCalls,
  localChatOf,
  saidBy,
  switchMode,
} from "@/lib/local-chat";

import { browserPaneOf } from "./browser-panes";

/**
 * The chat's folder and computer, and, in Surogate Desktop, the binding this computer holds for it,
 * read again at each change the desktop tells of, and when *reread* is called. Null for a chat in
 * the cloud. What the binding says of the browser when the chat is opened goes to its pane, which
 * tells the server where nobody holds it.
 */
export function useLocalChat(sessionId: string): {
  chat: LocalChat | null;
  desktop: FolderCalls | null;
  reread: () => void;
} {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [here, setHere] = useState<DesktopBinding | null>(null);
  const again = useRef<(() => void) | null>(null);
  const reread = useCallback(() => again.current?.(), []);
  const desktop = folderCalls(getDesktop());

  // ponytail: the chat and the computers are read once per chat opened (the page keys the bar by
  // its chat, so each chat's starts empty); a revocation made meanwhile shows at the next open.
  useEffect(() => {
    let live = true;
    let stop: (() => void) | undefined;
    getSession(sessionId).then(
      (session) => {
        const chat = localChatOf(sessionId, session.config, null, null);
        // A chat in the cloud has no bar, and asks nothing of the computers.
        if (!(live && chat)) {
          return;
        }
        setConfig(session.config);
        listDevices().then(
          (rows) => {
            if (live) {
              setDevices(rows);
            }
          },
          () => {
            // The computer is named as the chat's config names it.
          },
        );
        if (!desktop) {
          return;
        }
        // A sub-agent's chat works in its root's folder, which the binding names. Who holds the
        // browser, as the read made when the chat is opened says it (*opened*), goes to the chat's
        // pane, which tells the server once where nobody does: the bar reads it too, so a chat is
        // told though its pane was never drawn.
        const read = (opened = false) => {
          desktop.getBinding(chat.root).then(
            (binding) => {
              if (live) {
                setHere(binding);
                if (opened) {
                  browserPaneOf(sessionId).loaded(binding?.takenOver);
                }
              }
            },
            () => {
              if (live) {
                setHere(null);
              }
            },
          );
        };
        read(true);
        again.current = () => read();
        stop = desktop.onBindingChanged((changed) => {
          if (changed === chat.root) {
            read();
          }
        });
      },
      () => {
        // A chat that cannot be read has no bar.
      },
    );
    return () => {
      live = false;
      again.current = null;
      stop?.();
    };
  }, [sessionId, desktop]);

  return {
    chat: config ? localChatOf(sessionId, config, devices, here) : null,
    desktop,
    reread,
  };
}

export function LocalChatBar({ sessionId }: { sessionId: string }) {
  const [failure, setFailure] = useState<string | null>(null);
  const { chat, desktop } = useLocalChat(sessionId);
  if (!chat) {
    return null;
  }

  const run = (action: () => Promise<unknown>) => {
    setFailure(null);
    action().catch((error: unknown) => setFailure(saidBy(error)));
  };
  const mode = chat.here?.mode;

  return (
    <div
      data-testid="local-chat-bar"
      className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2 text-sm"
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <FolderIcon
          className="size-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <span className="font-medium">{chat.name}</span>
        <span className="truncate text-xs text-muted-foreground">
          {chat.folder}
        </span>
      </span>
      {desktop && chat.here && (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => run(() => desktop.revealFolder(chat.root))}
        >
          Show folder
        </Button>
      )}
      {chat.revoked ? (
        <span className="text-destructive">Local access revoked</span>
      ) : (
        desktop &&
        mode && (
          <>
            <span className="text-muted-foreground">
              {mode === "free" ? "Works freely" : "Asks every time"}
            </span>
            {/* One button, acted on when pressed: the desktop's own window confirms Work freely. */}
            <Button
              variant="ghost"
              size="sm"
              // The desktop keeps the mode it allows, and tells of each change: the mode shown is its.
              onClick={() =>
                run(() =>
                  switchMode(
                    desktop,
                    chat.root,
                    mode === "free" ? "ask" : "free",
                  ),
                )
              }
            >
              {mode === "free" ? "Ask every time" : "Let it work freely…"}
            </Button>
          </>
        )
      )}
      <span className="flex items-center gap-1.5 text-muted-foreground">
        <LaptopIcon className="size-4 shrink-0" aria-hidden="true" />
        on {chat.computer}
      </span>
      <span role="alert" className="text-destructive">
        {failure}
      </span>
    </div>
  );
}
