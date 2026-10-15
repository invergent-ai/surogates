// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Command as CommandPrimitive } from "cmdk";
import { SearchIcon } from "lucide-react";
import type { AgentChatSlashCommand } from "../../types";
import { PopoverContent } from "@invergent/ui-sdk/popover";
import { Command, CommandEmpty, CommandItem, CommandList } from "../ui/command";
import { Skeleton } from "@invergent/ui-sdk/skeleton";

// ── Modes ────────────────────────────────────────────────────────────
//
// One panel backs every entry point into the slash menu: the three
// expert-mode trigger buttons (``commands`` / ``skills`` / ``scheduled``)
// and free-form ``/`` typing in the textarea (``all``).  Keeping them in
// a single component means the search header, divider, list chrome and
// keyboard affordances stay identical no matter how the menu was opened.

export type ComposerMenuMode = "commands" | "skills" | "scheduled" | "all";

const SEARCH_PLACEHOLDER: Record<ComposerMenuMode, string> = {
  commands: "Search commands…",
  skills: "Search skills…",
  scheduled: "Search scheduled tasks…",
  all: "Type a command or search…",
};

// Skills is the one mode that routinely renders an empty state, so its
// hint is trimmed to the single gesture that still applies there; every
// other mode is a navigable list and advertises the full set.
const NAV_HINT = "↑↓ navigate   ·   Enter select   ·   Esc dismiss";
const SEARCH_HINT: Record<ComposerMenuMode, string> = {
  commands: NAV_HINT,
  skills: "Esc dismiss",
  scheduled: NAV_HINT,
  all: NAV_HINT,
};

interface ComposerCommandMenuProps {
  menuMode: ComposerMenuMode;
  /** Mirrors the textarea query; only read in the controlled modes. */
  searchQuery: string;
  onSearchChange: (next: string) => void;
  skillsLoading: boolean;
  builtinCommands: AgentChatSlashCommand[];
  adapterCommands: AgentChatSlashCommand[];
  scheduledExamples: AgentChatSlashCommand[];
  onCommandSelect: (value: string) => void;
  /** Return focus to the textarea after an Escape dismissal. */
  onEscapeDismiss: () => void;
}

// Shared row geometry: a fixed mono-name column + a flexible description.
// A phone gives the name column less, or the description gets nothing.
const NAME_COLUMNS = "grid-cols-[120px_minmax(0,1fr)] sm:grid-cols-[200px_minmax(0,1fr)]";
const ROW_CLASS = `grid ${NAME_COLUMNS} min-h-11 items-center gap-4 rounded-[10px] px-3 py-1.5 data-[selected=true]:bg-accent [&_svg]:hidden`;

export function ComposerCommandMenu({
  menuMode,
  searchQuery,
  onSearchChange,
  skillsLoading,
  builtinCommands,
  adapterCommands,
  scheduledExamples,
  onCommandSelect,
  onEscapeDismiss,
}: ComposerCommandMenuProps) {
  // The query mirrors back into the textarea only when the menu was
  // opened by typing ``/`` (``all``) or via the Commands button — the
  // skills / scheduled scopes run cmdk's own uncontrolled filtering so a
  // mirror write can't widen their scope back to "all".
  const controlledSearch = menuMode === "commands" || menuMode === "all";
  const showCommands = menuMode === "commands" || menuMode === "all";
  const showSkills = menuMode === "skills" || menuMode === "all";

  // Zero attached skills is a first-class state with its own call to
  // action, distinct from "your search matched nothing" (cmdk's empty).
  const skillsUnattached =
    menuMode === "skills" && !skillsLoading && adapterCommands.length === 0;

  return (
    <PopoverContent
      side="top"
      align="start"
      sideOffset={8}
      className="overflow-hidden rounded-[20px] border border-input bg-card p-0 shadow-lg"
      style={{ width: "var(--radix-popover-trigger-width)" }}
      onCloseAutoFocus={(e) => e.preventDefault()}
      // The toolbar's menu toggles sit outside this popup. Left to Radix, a
      // press on one would close the menu and its click open it again, so a
      // toggle could never close its own menu or switch it to another scope.
      onInteractOutside={(e) => {
        if ((e.target as Element | null)?.closest?.("[data-composer-menu-toggle]")) {
          e.preventDefault();
        }
      }}
      onEscapeKeyDown={() => {
        // Escape is the canonical "back out of the popup, keep typing in
        // the chat" gesture.  Click-outside is deliberately NOT routed
        // here — if the user clicked some other widget on the page they
        // expect focus to land wherever they clicked.
        onEscapeDismiss();
      }}
    >
      {/*
        Native cmdk command palette: the input owns focus while the popup
        is open, cmdk runs the filter (matching ``value`` plus the
        ``keywords`` we pass per item, so descriptions count toward
        matches) and handles arrow keys / Enter / scroll-into-view itself.
        In the controlled modes the input mirrors the textarea so the chat
        input still reflects the query and sending the message still works.
        Keyed by scope: a toggle can switch the open menu to another one,
        and the search typed for commands means nothing in the skills list.
      */}
      <Command key={menuMode}>
        {/* ── Search header ─────────────────────────────────────────── */}
        <div className="flex h-14 items-center gap-3 border-b border-border px-4.5">
          <SearchIcon className="size-4.5 shrink-0 text-muted-foreground" />
          <CommandPrimitive.Input
            data-slot="command-input"
            placeholder={SEARCH_PLACEHOLDER[menuMode]}
            className="h-10 min-w-0 flex-1 bg-transparent text-base text-foreground outline-hidden placeholder:text-muted-foreground"
            {...(controlledSearch
              ? { value: searchQuery, onValueChange: onSearchChange }
              : {})}
          />
          <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground max-sm:hidden">
            {SEARCH_HINT[menuMode]}
          </span>
        </div>

        {/* ── Body ──────────────────────────────────────────────────── */}
        {skillsUnattached ? (
          <SkillsEmptyState />
        ) : (
          // A fixed height, not content height: the popup must not jump as
          // the search narrows the list.
          <CommandList className="h-66 max-h-none px-2.5 py-2">
            {/*
              While skills are still being fetched ``adapterCommands`` is
              empty, so cmdk would flash its empty message until the
              request resolves. Suppress it and show skeleton rows so the
              menu reads as "loading" on its very first open.
            */}
            {!(skillsLoading && showSkills) && (
              <CommandEmpty className="px-3 py-8 text-center text-sm text-muted-foreground">
                {menuMode === "skills"
                  ? "No skills match your search."
                  : menuMode === "scheduled"
                    ? "No scheduled tasks found."
                    : "No commands found."}
              </CommandEmpty>
            )}

            {showSkills &&
              skillsLoading &&
              adapterCommands.length === 0 &&
              [0, 1, 2].map((i) => (
                <div
                  key={`skill-skeleton-${i}`}
                  className={`grid ${NAME_COLUMNS} min-h-11 items-center gap-4 px-3`}
                  aria-hidden="true"
                >
                  <Skeleton className="h-4 w-20" />
                  <Skeleton className="h-3 w-full max-w-[16rem]" />
                </div>
              ))}

            {menuMode === "scheduled" &&
              scheduledExamples.map((cmd) => (
                <MenuRow
                  key={cmd.value}
                  cmd={cmd}
                  onSelect={onCommandSelect}
                />
              ))}

            {showCommands &&
              builtinCommands.map((cmd) => (
                <MenuRow
                  key={cmd.value}
                  cmd={cmd}
                  onSelect={onCommandSelect}
                />
              ))}

            {showSkills &&
              adapterCommands.map((cmd) => (
                <MenuRow
                  key={cmd.value}
                  cmd={cmd}
                  // Including "expert" as a fuzzy-match keyword lets the
                  // user type "/expert" to list every specialist at once.
                  keywords={
                    cmd.isExpert ? [cmd.description, "expert"] : undefined
                  }
                  badge={cmd.isExpert ? "expert" : undefined}
                  onSelect={onCommandSelect}
                />
              ))}
          </CommandList>
        )}
      </Command>
    </PopoverContent>
  );
}

// ── Command row ──────────────────────────────────────────────────────

function MenuRow({
  cmd,
  keywords,
  badge,
  onSelect,
}: {
  cmd: AgentChatSlashCommand;
  keywords?: string[];
  badge?: string;
  onSelect: (value: string) => void;
}) {
  return (
    <CommandItem
      value={cmd.value}
      keywords={keywords ?? [cmd.description]}
      onSelect={() => onSelect(cmd.value)}
      className={ROW_CLASS}
    >
      <span
        className="inline-flex min-w-0 max-w-full items-center gap-1.5"
        title={cmd.label}
      >
        <span className="truncate font-mono text-sm font-medium text-amber-500 dark:text-amber-400">
          {cmd.label}
        </span>
        {badge && (
          <span
            className="shrink-0 rounded-sm bg-amber-500/10 px-1 text-[9px] font-semibold uppercase tracking-wider text-amber-500"
            aria-label="Expert specialist"
          >
            {badge}
          </span>
        )}
      </span>
      <span
        className="min-w-0 truncate text-sm text-muted-foreground"
        title={cmd.description}
      >
        {cmd.description}
      </span>
    </CommandItem>
  );
}

// ── Skills empty state ───────────────────────────────────────────────

function SkillsEmptyState() {
  return (
    <div className="flex flex-col items-center gap-2.5 px-6 py-[30px] text-center">
      <span
        className="text-2xl leading-none text-muted-foreground/70"
        aria-hidden="true"
      >
        ✦
      </span>
      <p className="text-sm font-medium text-foreground">No skills attached</p>
      <p className="text-[12.5px] text-muted-foreground">
        Add them in{" "}
        <span className="text-amber-500">Configure → Skills &amp; Tools</span>
      </p>
    </div>
  );
}
