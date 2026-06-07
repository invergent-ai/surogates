import { useCallback, useEffect, useState } from "react";
import type { AgentChatAdapter } from "../../types";

interface ToolkitRow {
  toolkit: string;
  connected: boolean;
}

export interface ConnectionsPanelProps {
  agentId?: string;
  adapter: AgentChatAdapter;
}

/**
 * Host-mounted panel listing the agent's Composio toolkits with per-end-user
 * connection status + a Connect action. Availability is set by an admin in
 * Ops; here each end-user connects their own provider account. Renders
 * nothing when the adapter doesn't implement the Composio methods or the
 * agent has no toolkits.
 */
export function ConnectionsPanel({ agentId, adapter }: ConnectionsPanelProps) {
  const supported =
    typeof adapter.listComposioConnections === "function" &&
    typeof adapter.authorizeComposioToolkit === "function";
  const [rows, setRows] = useState<ToolkitRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

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

  if (!supported || rows.length === 0) return null;

  const connect = async (toolkit: string) => {
    if (!adapter.authorizeComposioToolkit) return;
    setBusy(toolkit);
    try {
      const { redirectUrl } = await adapter.authorizeComposioToolkit({
        agentId,
        toolkit,
      });
      if (redirectUrl) window.open(redirectUrl, "_blank", "noopener,noreferrer");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-2 p-3">
      <div className="text-xs font-semibold text-muted-foreground">
        Connections
      </div>
      {rows.map((r) => (
        <div
          key={r.toolkit}
          className="flex items-center justify-between text-sm"
        >
          <span className="flex items-center gap-2">
            <span
              className={
                r.connected ? "text-emerald-500" : "text-muted-foreground/50"
              }
            >
              {r.connected ? "●" : "○"}
            </span>
            {r.toolkit}
          </span>
          {r.connected ? (
            <span className="text-xs text-emerald-500">connected</span>
          ) : (
            <button
              type="button"
              onClick={() => connect(r.toolkit)}
              disabled={busy === r.toolkit}
              className="rounded bg-violet-600 px-2 py-0.5 text-xs text-white disabled:opacity-50"
            >
              {busy === r.toolkit ? "Opening…" : "Connect"}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
