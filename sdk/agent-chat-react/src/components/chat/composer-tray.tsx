// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useState } from "react";
import { Button } from "@invergent/ui-sdk/button";
import {
  ChevronDownIcon,
  FolderIcon,
  GlobeIcon,
  Maximize2Icon,
  Minimize2Icon,
  PanelTopIcon,
  RefreshCwIcon,
  type LucideIcon,
} from "lucide-react";
import { cn } from "../../lib/utils";

/**
 * What the session has produced, offered at the top of the composer card.
 *
 * One tab per resource over one fold: the tabs pick what the fold shows and
 * the chevron opens or closes it. The fold is part of the card rather than a
 * panel stacked behind it, so the composer stays one surface.
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

type TrayTab = "files" | "browser";

// 44px targets, the size every control in the card is built to.
const ICON_BUTTON = "size-11";

export function ComposerTray({ browser, files, open, onOpenChange }: ComposerTrayProps) {
  const [picked, setPicked] = useState<TrayTab>("files");
  // A tab whose resource has gone (a browser destroyed mid-session) falls
  // back to the one that remains rather than folding open onto nothing.
  const tab: TrayTab =
    picked === "browser" ? (browser ? "browser" : "files") : files ? "files" : "browser";

  // The open tab closes the fold; any other opens it on itself.
  const pick = (next: TrayTab) => {
    setPicked(next);
    onOpenChange(!(open && tab === next));
  };

  return (
    <div data-testid="composer-tray" className="w-full border-b border-border">
      <div className="flex h-14 items-center gap-1 pr-2 pl-2.5">
        {files && (
          <TrayTabButton
            testId="composer-tray-files"
            icon={FolderIcon}
            label="Files"
            selected={open && tab === "files"}
            onClick={() => pick("files")}
          />
        )}
        {browser && (
          <TrayTabButton
            testId="composer-tray-browser"
            icon={GlobeIcon}
            label="Browser"
            selected={open && tab === "browser"}
            onClick={() => pick("browser")}
          />
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon-lg"
          aria-expanded={open}
          aria-label={open ? "Hide panel" : "Show panel"}
          onClick={() => onOpenChange(!open)}
          className={cn(ICON_BUTTON, "ml-auto")}
        >
          <ChevronDownIcon
            className={cn("size-4 transition-transform", open && "rotate-180")}
            aria-hidden="true"
          />
        </Button>
      </div>
      {open && tab === "files" && files && (
        // A fixed height, not content height: the tree scrolls inside it, and
        // the textarea must stay put when a folder with a hundred files opens.
        <div
          data-testid="files-accordion"
          className="flex h-56 min-h-0 flex-col border-t border-border"
        >
          {files.panel}
        </div>
      )}
      {open && tab === "browser" && browser && <BrowserFold {...browser} />}
    </div>
  );
}

function TrayTabButton({
  testId,
  icon: Icon,
  label,
  selected,
  onClick,
}: {
  testId: string;
  icon: LucideIcon;
  label: string;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        "inline-flex h-11 items-center gap-2 rounded-xl px-3.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-400",
        selected
          ? "bg-accent text-foreground"
          : "text-foreground/75 hover:bg-accent hover:text-foreground",
      )}
    >
      <Icon className="size-4.5 text-muted-foreground" strokeWidth={1.8} aria-hidden="true" />
      {label}
    </button>
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
    <div data-testid="browser-fold" className="flex flex-col border-t border-border">
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
