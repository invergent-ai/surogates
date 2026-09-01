import { ChevronDownIcon, GlobeIcon, type LucideIcon } from "lucide-react";
import { cn } from "../../lib/utils";

/**
 * A resource the session has produced, offered above the composer.
 *
 * The right column starts closed, so these cards are how a viewer gets to it:
 * one per pane, each opening the column on its own tab. The browser card
 * carries a thumbnail because "what is on the page" is the thing you actually
 * want at a glance; the files card carries a count for the same reason.
 */

interface SessionPaneCardProps {
  title: string;
  /** One line of context — the page being visited, the folder being written. */
  subtitle?: string;
  /** Latest preview image, or null until the first poll answers. */
  thumbnail?: string | null;
  /** Badge value. Hidden at zero: an empty workspace is not worth a chip. */
  count?: number;
  icon?: LucideIcon;
  onOpen: () => void;
  testId?: string;
  /** Lets a caller restyle the shape when cards are stacked into one surface. */
  className?: string;
  /**
   * Defined when the card is an accordion header: it gets a trailing chevron
   * and announces its state, and onOpen becomes a toggle.
   */
  expanded?: boolean;
}

export function SessionPaneCard({
  title,
  subtitle,
  thumbnail,
  count,
  icon: Icon = GlobeIcon,
  onOpen,
  testId = "session-pane-card",
  className,
  expanded,
}: SessionPaneCardProps) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-label={`Open ${title}`}
      aria-expanded={expanded === undefined ? undefined : expanded}
      onClick={onOpen}
      className={cn(
        // No radius here: the caller sets it. Declaring rounded-xl in the base
        // and cancelling the bottom from a className leaves both rules live,
        // and which corner wins comes down to stylesheet order rather than
        // intent -- which is how the stacked cards kept round bottoms.
        "flex w-full items-center gap-3 border border-line px-3 py-3.5 text-left transition-colors hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    >
      {thumbnail ? (
        <img
          data-testid="session-pane-card-thumb"
          src={thumbnail}
          alt=""
          className="h-11 w-16 shrink-0 rounded-md border border-line object-cover object-top"
        />
      ) : (
        <Icon
          data-testid="session-pane-card-icon"
          className="size-5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      )}
      <span className="flex min-w-0 flex-col">
        <span className="flex items-center gap-1.5 text-sm font-medium text-foreground">
          {title}
          {count !== undefined && count > 0 && (
            <span className="rounded-full bg-secondary px-1.5 text-[11px] font-normal text-muted-foreground">
              {count}
            </span>
          )}
        </span>
        {subtitle && (
          <span className="truncate text-xs text-muted-foreground">
            {subtitle}
          </span>
        )}
      </span>
      {expanded !== undefined && (
        <ChevronDownIcon
          data-testid="session-pane-card-chevron"
          className={cn(
            "ml-auto size-4 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-180",
          )}
          aria-hidden="true"
        />
      )}
    </button>
  );
}
