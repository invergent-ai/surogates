// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Button } from "@invergent/ui-sdk/button";
import {
  Maximize2Icon,
  Minimize2Icon,
  PanelTopIcon,
  RefreshCwIcon,
} from "lucide-react";

/**
 * What the session has produced, offered in the composer card.
 *
 * One icon per resource in the composer's row over one fold above the message:
 * an icon opens the fold on its resource and closes it when it already shows.
 * The fold is part of the card rather than a panel stacked behind it, so the
 * composer stays one surface.
 */
export interface ComposerTrayProps {
  browser?: {
    /** Where the browser is or who holds it, when that is worth saying. */
    subtitle?: string;
    /** Latest preview image, or null until the first poll answers. */
    thumbnail?: string | null;
    updatedAt?: Date | null;
    /** Absent where there is nothing to photograph, as on the user's computer. */
    onRefresh?: () => void;
    /** Whether the browser pane beside the chat is showing. */
    showing: boolean;
    /** Shows or hides that pane. */
    onOpen: () => void;
  } | null;
  /** `panel` is the workspace tree the fold shows on the Files tab. */
  files?: { panel: React.ReactNode } | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export type TrayTab = "files" | "browser";

// 44px targets, the size every control in the fold is built to.
const ICON_BUTTON = "size-11";

/**
 * The tab that shows: the one picked, or, when its resource has gone (a browser
 * destroyed mid-session), the one that remains.
 */
export function trayTab(
  picked: TrayTab,
  { browser, files }: Pick<ComposerTrayProps, "browser" | "files">,
): TrayTab {
  return picked === "browser" ? (browser ? "browser" : "files") : files ? "files" : "browser";
}

/**
 * The tray's fold, above the message: the workspace tree or the browser's
 * preview. Its tabs are icons in the composer's row.
 */
export function ComposerTrayFold({
  browser,
  files,
  open,
  tab,
}: ComposerTrayProps & { tab: TrayTab }) {
  return (
    <div data-testid="composer-tray" className="w-full">
      {open && tab === "files" && files && (
        // A fixed height, not content height: the tree scrolls inside it, and
        // the textarea must stay put when a folder with a hundred files opens.
        <div
          data-testid="files-accordion"
          className="flex h-56 min-h-0 flex-col border-b border-border"
        >
          {files.panel}
        </div>
      )}
      {open && tab === "browser" && browser && <BrowserFold {...browser} />}
    </div>
  );
}

function BrowserFold({
  subtitle,
  thumbnail,
  updatedAt,
  onRefresh,
  showing,
  onOpen,
}: NonNullable<ComposerTrayProps["browser"]>) {
  const openLabel = showing ? "Hide the browser" : "Open the browser";
  return (
    <div data-testid="browser-fold" className="flex flex-col border-b border-border">
      <div className="flex h-12 items-center gap-1 pr-2.5 pl-[18px]">
        <span className="min-w-0 flex-1 truncate text-[13px] text-muted-foreground">
          {subtitle}
          {subtitle && updatedAt && " · "}
          {updatedAt && (
            <span className="font-mono text-xs">
              {subtitle ? "updated" : "Updated"}{" "}
              {updatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </span>
          )}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon-lg"
          aria-label={openLabel}
          aria-pressed={showing}
          onClick={onOpen}
          className={ICON_BUTTON}
        >
          {showing ? (
            <Minimize2Icon className="size-4.5" aria-hidden="true" />
          ) : (
            <Maximize2Icon className="size-4.5" aria-hidden="true" />
          )}
        </Button>
        {onRefresh && (
          <Button
            type="button"
            variant="ghost"
            size="icon-lg"
            aria-label="Refresh screenshot"
            onClick={onRefresh}
            className={ICON_BUTTON}
          >
            <RefreshCwIcon className="size-4.5" aria-hidden="true" />
          </Button>
        )}
      </div>
      <div className="flex h-44 border-t border-border px-[18px] pt-3 pb-3.5">
        {thumbnail ? (
          <button
            type="button"
            aria-label={openLabel}
            onClick={onOpen}
            className="min-h-0 flex-1 overflow-hidden rounded-xl border border-input focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-400"
          >
            <img
              src={thumbnail}
              alt=""
              className="size-full object-cover object-top"
            />
          </button>
        ) : (
          <div className="grid flex-1 place-items-center rounded-xl border border-dashed border-input bg-background px-4 text-center">
            <span className="flex flex-col items-center gap-1.5 text-[13px] text-muted-foreground">
              <PanelTopIcon className="size-6" strokeWidth={1.6} aria-hidden="true" />
              No screenshot yet
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
