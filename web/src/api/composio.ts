// Raw fetch wrappers for the /v1/composio REST surface. The web app calls
// the v1 routes through the /api/v1 proxy prefix.
import { authFetch } from "./auth";

export interface ComposioConnections {
  toolkits: { toolkit: string; connected: boolean }[];
}

export async function listComposioConnections(): Promise<ComposioConnections> {
  const r = await authFetch("/api/v1/composio/connections");
  if (!r.ok) {
    throw new Error("Failed to load connections");
  }
  return (await r.json()) as ComposioConnections;
}

export async function authorizeComposioToolkit(
  toolkit: string,
): Promise<{ redirectUrl: string; status: string }> {
  const r = await authFetch(
    `/api/v1/composio/toolkits/${encodeURIComponent(toolkit)}/authorize`,
    { method: "POST" },
  );
  if (!r.ok) {
    const e = (await r.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(e?.detail ?? "Failed to authorize toolkit");
  }
  const j = (await r.json()) as { redirect_url: string; status: string };
  return { redirectUrl: j.redirect_url, status: j.status };
}
