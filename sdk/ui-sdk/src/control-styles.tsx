// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Class strings for the Work console's compact form controls (its coding,
// SSH, repo and follow-up settings), for markup that cannot be a component —
// a native <select>, a bare <button> in a dense row. A .tsx only so the
// build publishes it like every other entry ("@invergent/ui-sdk/control-styles").
//
// The `min-h-11` / `pointer-fine:min-h-0` pairing is the pattern throughout: phones get
// the 44px iOS minimum, desktop keeps the original compact density.

export const CONTROL_FIELD =
  "w-full rounded-[10px] border border-foreground/10 bg-foreground/[0.04] px-3 py-2.5 text-[12px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/50 focus:border-foreground/20 focus:bg-foreground/[0.06] disabled:cursor-not-allowed disabled:opacity-70 sm:py-2";

export const CONTROL_PRIMARY_BTN =
  "inline-flex min-h-11 items-center justify-center gap-1.5 rounded-[9px] bg-foreground/[0.08] px-4 text-[13px] font-semibold text-foreground transition-colors disabled:cursor-not-allowed disabled:opacity-45 pointer-fine:min-h-0 sm:py-2 sm:hover:bg-foreground/[0.12] sm:disabled:hover:bg-foreground/[0.08]";

export const CONTROL_OUTLINE_BTN =
  "inline-flex min-h-11 items-center justify-center gap-1.5 rounded-[9px] border border-foreground/15 px-4 text-[13px] font-semibold text-foreground transition-colors disabled:cursor-not-allowed disabled:opacity-50 pointer-fine:min-h-0 sm:py-2 sm:hover:bg-foreground/[0.05]";

export const CONTROL_REMOVE_BTN =
  "inline-flex min-h-11 shrink-0 items-center justify-center rounded-[8px] px-3 text-[12px] font-medium text-muted-foreground transition-colors disabled:cursor-not-allowed disabled:opacity-50 pointer-fine:min-h-0 sm:px-2.5 sm:py-1.5 sm:hover:bg-destructive/10 sm:hover:text-destructive";

// A native <select>'s dropdown list is painted by the OS, which ignores the
// page's theme, so each <option> has to restate the tokens itself. One
// string rather than one per element: a token rename should not be a
// find-and-replace across a file.
export const CONTROL_OPTION = "bg-background text-foreground";
