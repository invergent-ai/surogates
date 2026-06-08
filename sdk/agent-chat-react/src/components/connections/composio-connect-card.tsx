import { PlugZapIcon } from "lucide-react";
import { openOAuthPopup } from "../../lib/oauth-popup";

export interface ComposioConnectCardProps {
  /** The Composio hosted connect URL (connect.composio.dev/link/…). */
  url: string;
  /** The original link text, e.g. "Connect Jira". */
  label?: string;
}

/** Derive a toolkit display name from the link label ("Connect Jira" → "Jira"). */
function toolkitName(label: string | undefined): string {
  const text = (label ?? "").trim();
  const stripped = text.replace(/^connect\s+/i, "").trim();
  return stripped || "your account";
}

/**
 * Rendered in place of a raw ``connect.composio.dev`` markdown link in an
 * assistant message: a tidy card with the toolkit name and a Connect button
 * that opens the hosted auth flow in a popup. Works for any Composio toolkit.
 */
export function ComposioConnectCard({ url, label }: ComposioConnectCardProps) {
  const name = toolkitName(label);
  return (
    <span className="my-1 inline-flex w-full max-w-sm items-center gap-3 rounded-lg border border-border bg-card p-3 align-middle no-underline">
      <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
        <PlugZapIcon className="h-4 w-4" />
      </span>
      <span className="flex min-w-0 flex-col">
        <span className="text-sm font-medium text-foreground">Connect {name}</span>
        <span className="text-xs text-muted-foreground">
          Authorize your {name} account to continue.
        </span>
      </span>
      <button
        type="button"
        onClick={() => openOAuthPopup(url)}
        className="ml-auto shrink-0 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
      >
        Connect
      </button>
    </span>
  );
}
