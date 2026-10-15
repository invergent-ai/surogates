import { ArrowLeftIcon, Loader2Icon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@invergent/ui-sdk/button";
import { TagPill } from "@invergent/ui-sdk/chip";
import { Collapsible, CollapsibleContent } from "@invergent/ui-sdk/collapsible";
import { CollapsibleHeader } from "@invergent/ui-sdk/collapsible-header";
import { Input } from "@invergent/ui-sdk/input";
import { SectionLabel } from "@invergent/ui-sdk/section-label";
import { TextLink } from "@invergent/ui-sdk/text-link";
import { openOAuthPopup } from "../../lib/oauth-popup";
import type { AgentChatAdapter } from "../../types";

interface Row {
  toolkit: string;
  connected: boolean;
  name?: string;
  logo?: string;
  category?: string;
  description?: string;
  auth_mode?: string;
}

export interface IntegrationsPageProps {
  agentId?: string;
  adapter: AgentChatAdapter;
  onBack: () => void;
}

const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 180_000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

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

  // Lets the detached connection poll stop touching state after unmount.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

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

  // Poll connection status until the toolkit reports connected, the popup is
  // closed by the user, or we time out — then refresh the page so the row
  // flips to Connected without a manual reload, and close the popup.
  const waitForConnection = async (toolkit: string, popup: Window | null) => {
    if (!adapter.listComposioConnections) return;
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await sleep(POLL_INTERVAL_MS);
      if (!mountedRef.current) return;
      let toolkits: Row[] | null = null;
      try {
        const res = await adapter.listComposioConnections({ agentId });
        toolkits = res.toolkits;
        if (!mountedRef.current) return;
        setRows(res.toolkits);
      } catch {
        // Transient error — keep polling.
      }
      const connected = toolkits?.find((t) => t.toolkit === toolkit)?.connected;
      if (connected) {
        popup?.close();
        return;
      }
      // User closed the popup (we already refreshed above); stop polling.
      if (popup?.closed) return;
    }
  };

  const connect = async (toolkit: string) => {
    if (!adapter.authorizeComposioToolkit) return;
    setBusy(toolkit);
    setError(null);
    try {
      const { redirectUrl } = await adapter.authorizeComposioToolkit({
        agentId,
        toolkit,
      });
      const popup = redirectUrl ? openOAuthPopup(redirectUrl) : null;
      // Keep the button in its loading state while we poll, so the row flips
      // to Connected (with the popup auto-closed) as soon as the OAuth
      // handshake completes — no manual refresh/close needed. Resolves early
      // when the user closes the popup.
      await waitForConnection(toolkit, popup);
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
      <TextLink asChild variant="muted">
        <button type="button" onClick={onBack}>
          <ArrowLeftIcon className="size-4" />
          Back
        </button>
      </TextLink>
      <div>
        <h1 className="text-2xl font-bold text-foreground">Integrations</h1>
        <p className="text-sm text-muted-foreground">
          Connect third-party services to enhance your agent's capabilities
        </p>
      </div>
      <Input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search integrations..."
      />
      {error && (
        <div role="alert" className="text-xs text-red-500">
          {error}
        </div>
      )}
      {grouped.map(([category, items]) => (
        <section key={category} className="flex flex-col gap-2">
          <SectionLabel label={category} role="heading" aria-level={2} className="pl-0" />
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
    // A boxed disclosure: the header names the toolkit and opens its
    // description; Connect sits beside it, outside the trigger, since a
    // button cannot hold another.
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="rounded-[10px] border border-foreground/[0.08]"
    >
      <div className="flex items-center gap-2 pr-3">
        <CollapsibleHeader
          variant="section"
          aria-label={open ? "Collapse" : "Expand"}
          className="min-w-0 flex-1"
        >
          <span className="flex min-w-0 items-center gap-2">
            {broken || !row.logo ? (
              <span className="inline-flex size-5 shrink-0 items-center justify-center rounded bg-muted text-sm font-semibold uppercase">
                {name.charAt(0)}
              </span>
            ) : (
              <img
                src={row.logo}
                alt={name}
                className="size-5 shrink-0 rounded object-contain"
                onError={() => setBroken(true)}
              />
            )}
            <span className="truncate">{name}</span>
          </span>
        </CollapsibleHeader>
        <Button
          type="button"
          variant="outline"
          size="xs"
          disabled={busy}
          onClick={row.connected ? onDisconnect : onConnect}
          className="min-w-[6rem]"
        >
          {busy ? (
            <>
              <Loader2Icon className="size-3.5 animate-spin" />
              {row.connected ? "Disconnecting" : "Connecting"}
            </>
          ) : row.connected ? (
            "Disconnect"
          ) : (
            "Connect"
          )}
        </Button>
      </div>
      <CollapsibleContent className="border-t border-foreground/[0.08] px-3 py-2.5 text-[13px] text-muted-foreground">
        {row.description && <p>{row.description}</p>}
        <TagPill
          label={row.auth_mode === "api_key" ? "API key" : "OAuth"}
          className="mt-1.5"
        />
      </CollapsibleContent>
    </Collapsible>
  );
}
