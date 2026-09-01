import { GlobeIcon, type LucideIcon } from "lucide-react";
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
}: SessionPaneCardProps) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-label={`Open ${title}`}
      onClick={onOpen}
      className={cn(
        "flex w-full items-center gap-3 rounded-xl border border-line bg-card px-3 py-2.5 text-left transition-colors hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
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
        <span
          data-testid="session-pane-card-icon"
          className="flex h-11 w-16 shrink-0 items-center justify-center rounded-md border border-line bg-background"
        >
          <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
        </span>
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
    </button>
  );
}
