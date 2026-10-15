// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Shared stat-tile primitives used by the agent Metrics tab and the
// agent Users page: label / big value / baseline hint / bottom-pinned
// detail, with an optional info tooltip on the label.

import { Info } from "lucide-react";
import type { ReactNode } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./tooltip";
import { cn } from "./lib/utils";

export function InfoHint({ text }: { text: ReactNode }) {
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            aria-label="More info"
            tabIndex={0}
            className="inline-flex cursor-help text-muted-foreground/60"
          >
            <Info className="size-3.5" />
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-72 text-[12px] leading-relaxed">
          {text}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function StatCard({
  label,
  info,
  value,
  hint,
  detail,
  className,
  children,
}: {
  label: string;
  info?: ReactNode;
  value?: ReactNode;
  hint?: ReactNode;
  detail?: ReactNode;
  /** Card-shell classes (border, background, padding). Omit to render
   *  the bare tile inside an existing card. */
  className?: string;
  children?: ReactNode;
}) {
  return (
    // Label above value, always. Putting them on one line to save height
    // only works while the tile is full-width; inside the 2-up phone grid
    // the label wraps and the value collides with the info icon, which is
    // how this tile ended up looking broken on the Metrics tab.
    <div data-slot="stat-card" className={cn("flex h-full flex-col", className)}>
      <div className="flex items-center gap-1.5">
        <p className="text-[11.5px] font-medium uppercase leading-tight tracking-wide text-muted-foreground/80">
          {label}
        </p>
        {info && <InfoHint text={info} />}
      </div>
      {value !== undefined && (
        <div className="mt-1.5 flex items-baseline gap-2">
          <span className="text-[22px] font-semibold leading-tight text-foreground">
            {value}
          </span>
          {hint && (
            <span className="text-[12px] text-muted-foreground">{hint}</span>
          )}
        </div>
      )}
      {children}
      {detail && (
        <div className="mt-auto pt-1.5 text-[12px] leading-snug text-muted-foreground">
          {detail}
        </div>
      )}
    </div>
  );
}

export function ChannelChip({ label }: { label: string }) {
  return (
    <span
      data-slot="channel-chip"
      className="inline-flex items-center rounded-[6px] bg-foreground/[0.07] px-1.5 py-0.5 text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground"
    >
      {label}
    </span>
  );
}
