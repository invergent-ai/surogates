// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Over a chat on a folder of the user's computer: the folder, the computer, and, in Surogate
// Desktop on that computer, Show folder and the chat's mode (Section 8). In a browser it names
// the folder and the computer only: the mode is the computer's to keep.
import { FolderIcon, LaptopIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { type Device, listDevices } from "@/api/devices";
import { getSession } from "@/api/sessions";
import { Button } from "@/components/ui/button";
import { type DesktopBinding, getDesktop } from "@/lib/desktop-bridge";
import { folderCalls, localChatOf, saidBy, switchMode } from "@/lib/local-chat";

export function LocalChatBar({ sessionId }: { sessionId: string }) {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [here, setHere] = useState<DesktopBinding | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
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
        // A sub-agent's chat works in its root's folder, which the binding names.
        const read = () => {
          desktop.getBinding(chat.root).then(
            (binding) => {
              if (live) {
                setHere(binding);
              }
            },
            () => {
              if (live) {
                setHere(null);
              }
            },
          );
        };
        read();
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
      stop?.();
    };
  }, [sessionId, desktop]);

  const chat = config ? localChatOf(sessionId, config, devices, here) : null;
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
