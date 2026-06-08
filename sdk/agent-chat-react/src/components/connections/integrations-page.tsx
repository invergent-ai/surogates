import { useCallback, useEffect, useMemo, useState } from "react";
import type { AgentChatAdapter } from "../../types";

interface Row {
  toolkit: string;
  connected: boolean;
  name?: string;
  logo?: string;
  category?: string;
  description?: string;
}

export interface IntegrationsPageProps {
  agentId?: string;
  adapter: AgentChatAdapter;
  onBack: () => void;
}

/**
 * Open the provider OAuth URL in a centered popup window (not a new tab).
 * Falls back to a normal navigation if the popup is blocked.
 */
function openOAuthPopup(url: string): void {
  const w = 600;
  const h = 720;
  const dualLeft = window.screenLeft ?? window.screenX ?? 0;
  const dualTop = window.screenTop ?? window.screenY ?? 0;
  const width = window.innerWidth || document.documentElement.clientWidth || w;
  const height =
    window.innerHeight || document.documentElement.clientHeight || h;
  const left = dualLeft + Math.max(0, (width - w) / 2);
  const top = dualTop + Math.max(0, (height - h) / 2);
  const features = `popup=yes,width=${w},height=${h},left=${left},top=${top},noopener,noreferrer`;
  const popup = window.open(url, "composio-oauth", features);
  if (!popup) {
    // Popup blocked — fall back so the flow still completes.
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

function titleCase(slug: string): string {
  return slug
    .split(/[_-]/)
    .filter(Boolean)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
}

/**
 * Full-screen Integrations view: search + category groups + per-toolkit
 * Connect/Disconnect. Lists the agent's assigned toolkits (from the adapter),
 * rendering whatever metadata the backend enriched the rows with.
 */
export function IntegrationsPage({ agentId, adapter, onBack }: IntegrationsPageProps) {
  const [rows, setRows] = useState<Row[]>([]);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!adapter.listComposioConnections) return;
    const res = await adapter.listComposioConnections({ agentId });
    setRows(res.toolkits);
  }, [adapter, agentId]);

  useEffect(() => {
    void refresh();
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = rows.filter((r) =>
      (r.name ?? titleCase(r.toolkit)).toLowerCase().includes(q),
    );
    const byCat = new Map<string, Row[]>();
    for (const r of filtered) {
      const cat = r.category || "Other";
      const list = byCat.get(cat) ?? [];
      list.push(r);
      byCat.set(cat, list);
    }
    return Array.from(byCat.entries());
  }, [rows, query]);

  const connect = async (toolkit: string) => {
    if (!adapter.authorizeComposioToolkit) return;
    setBusy(toolkit);
    setError(null);
    try {
      const { redirectUrl } = await adapter.authorizeComposioToolkit({
        agentId,
        toolkit,
      });
      if (redirectUrl) openOAuthPopup(redirectUrl);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start the connection");
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (toolkit: string) => {
    if (!adapter.disconnectComposioToolkit) return;
    setBusy(toolkit);
    setError(null);
    try {
      await adapter.disconnectComposioToolkit({ agentId, toolkit });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to disconnect");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
      <button
        type="button"
        onClick={onBack}
        className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        ← Back
      </button>
      <div>
        <h1 className="text-2xl font-bold text-foreground">Integrations</h1>
        <p className="text-sm text-muted-foreground">
          Connect third-party services to enhance your agent's capabilities
        </p>
      </div>
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search integrations..."
        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
      />
      {error && (
        <div role="alert" className="text-xs text-red-500">
          {error}
        </div>
      )}
      {grouped.map(([category, items]) => (
        <section key={category} className="flex flex-col gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
            {category}
          </h2>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {items.map((r) => (
              <ToolkitRow
                key={r.toolkit}
                row={r}
                busy={busy === r.toolkit}
                onConnect={() => connect(r.toolkit)}
                onDisconnect={() => disconnect(r.toolkit)}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function ToolkitRow({
  row,
  busy,
  onConnect,
  onDisconnect,
}: {
  row: Row;
  busy: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [broken, setBroken] = useState(false);
  const name = row.name ?? titleCase(row.toolkit);
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label={open ? "Collapse" : "Expand"}
          onClick={() => setOpen((v) => !v)}
          className="text-muted-foreground/60"
        >
          {open ? "▾" : "›"}
        </button>
        {broken || !row.logo ? (
          <span className="inline-flex h-5 w-5 items-center justify-center rounded bg-muted text-[10px] font-semibold uppercase">
            {name.charAt(0)}
          </span>
        ) : (
          <img
            src={row.logo}
            alt={name}
            className="h-5 w-5 rounded object-contain"
            onError={() => setBroken(true)}
          />
        )}
        <span className="text-sm font-medium text-foreground">{name}</span>
        {row.connected ? (
          <button
            type="button"
            disabled={busy}
            onClick={onDisconnect}
            className="ml-auto rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-accent disabled:opacity-50"
          >
            {busy ? "…" : "Disconnect"}
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={onConnect}
            className="ml-auto rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-accent disabled:opacity-50"
          >
            {busy ? "…" : "Connect"}
          </button>
        )}
      </div>
      {open && (
        <div className="mt-2 pl-7 text-xs text-muted-foreground">
          {row.description && <p>{row.description}</p>}
          <span className="mt-1 inline-block rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
            OAuth
          </span>
        </div>
      )}
    </div>
  );
}
