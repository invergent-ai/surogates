import { ChevronRightIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { AgentChatAdapter } from "../../types";

interface ToolkitRow {
  toolkit: string;
  connected: boolean;
  name?: string;
  logo?: string;
}

export interface IntegrationsBandProps {
  agentId?: string;
  adapter: AgentChatAdapter;
  onOpenIntegrations: () => void;
}

const MAX_LOGOS = 10;

/**
 * Thin strip under the composer. Shows up to ten connected toolkit logos and
 * a "Connect your integrations" prompt; the whole band opens the Integrations
 * page. Renders nothing when the adapter lacks the Composio methods or the
 * agent has no assigned toolkits.
 */
export function IntegrationsBand({
  agentId,
  adapter,
  onOpenIntegrations,
}: IntegrationsBandProps) {
  const supported =
    typeof adapter.listComposioConnections === "function" &&
    typeof adapter.authorizeComposioToolkit === "function";
  const [rows, setRows] = useState<ToolkitRow[] | null>(null);

  const refresh = useCallback(async () => {
    if (!adapter.listComposioConnections) return;
    const res = await adapter.listComposioConnections({ agentId });
    setRows(res.toolkits);
  }, [adapter, agentId]);

  useEffect(() => {
    if (!supported) return;
    void refresh();
    // Re-fetch when the user returns from the OAuth popup/tab.
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [supported, refresh]);

  // null = not loaded yet; [] = loaded, agent has no toolkits.
  if (!supported || rows === null || rows.length === 0) return null;

  // Show every assigned toolkit's logo (connected ones full-colour,
  // unconnected ones dimmed) so the strip always previews what's available.
  const shown = rows.slice(0, MAX_LOGOS);

  return (
    <div className="px-5">
    <button
      type="button"
      onClick={onOpenIntegrations}
      className="flex w-full items-center gap-2 border border-border border-t-0 rounded-b-lg px-4 py-2.5 text-xs text-muted-foreground transition-colors hover:bg-accent/40 cursor-pointer"
    >
      <span className="font-medium">Connect your integrations</span>
      <span className="ml-auto flex items-center gap-1.5">
        {shown.map((r) => (
          <Logo key={r.toolkit} row={r} />
        ))}
        <ChevronRightIcon
          aria-hidden
          className="h-3.5 w-3.5 text-muted-foreground/60"
        />
      </span>
    </button>
    </div>
  );
}

function Logo({ row }: { row: ToolkitRow }) {
  const [broken, setBroken] = useState(false);
  const label = row.name ?? row.toolkit;
  if (broken || !row.logo) {
    return (
      <span
        title={label}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full bg-muted text-[8px] font-semibold uppercase"
      >
        {label.charAt(0)}
      </span>
    );
  }
  return (
    <img
      src={row.logo}
      alt={label}
      title={label}
      className="h-4 w-4 rounded-sm object-contain"
      onError={() => setBroken(true)}
    />
  );
}
