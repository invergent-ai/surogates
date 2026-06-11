// Raw fetch wrappers for the /v1/coding-agents REST surface. The web app
// calls the v1 routes through the /api/v1 proxy prefix. On non-OK responses
// we surface the FastAPI `detail` string so the panel shows the validator's
// user-facing message (e.g. a 422 "that doesn't look like a setup token").
import { authFetch } from "./auth";

export interface CodingAgentConnection {
  provider: "anthropic" | "openai";
  connected: boolean;
  auth_mode: "oauth" | "api_key" | null;
  expires_at: number | null;
}

export interface CodingAgentConnections {
  connections: CodingAgentConnection[];
}

async function detail(r: Response, fallback: string): Promise<string> {
  const e = (await r.json().catch(() => null)) as { detail?: string } | null;
  return e?.detail ?? fallback;
}

export async function listCodingAgentConnections(): Promise<CodingAgentConnections> {
  const r = await authFetch("/api/v1/coding-agents/connections");
  if (!r.ok) {
    throw new Error(await detail(r, "Failed to load coding-agent connections"));
  }
  return (await r.json()) as CodingAgentConnections;
}

export async function submitCodingAgentCredential(
  provider: string,
  mode: "oauth" | "api_key",
  value: string,
): Promise<{ provider: string; connected: boolean; auth_mode: string }> {
  const r = await authFetch(
    `/api/v1/coding-agents/${encodeURIComponent(provider)}/credential`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, value }),
    },
  );
  if (!r.ok) {
    throw new Error(await detail(r, "Failed to save the credential"));
  }
  return (await r.json()) as {
    provider: string;
    connected: boolean;
    auth_mode: string;
  };
}

export async function disconnectCodingAgentProvider(provider: string): Promise<void> {
  const r = await authFetch(
    `/api/v1/coding-agents/${encodeURIComponent(provider)}`,
    { method: "DELETE" },
  );
  if (!r.ok) {
    throw new Error(await detail(r, "Failed to disconnect"));
  }
}
