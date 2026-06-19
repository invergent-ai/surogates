// Client for the per-user browser-profile routes. The web app authenticates
// as a real harness user (JWT), so the harness scopes profiles by user id —
// no agent transport is needed. Calls go to ``/api/v1/...``; the dev proxy
// strips ``/api`` so they reach the harness as ``/v1/browser-profiles``.
import { authFetch } from "@/api/auth";

export interface BrowserProfile {
  id: string;
  name: string;
  source: string;
  cookieDomains: string[];
  hasState: boolean;
  createdAt: string;
  lastUsedAt: string | null;
}

interface RawProfile {
  id: string;
  name: string;
  source: string;
  cookie_domains: string[];
  has_state: boolean;
  created_at: string;
  last_used_at: string | null;
}

const toProfile = (p: RawProfile): BrowserProfile => ({
  id: p.id,
  name: p.name,
  source: p.source,
  cookieDomains: p.cookie_domains,
  hasState: p.has_state,
  createdAt: p.created_at,
  lastUsedAt: p.last_used_at,
});

async function json<T>(url: string, msg: string, init?: RequestInit): Promise<T> {
  const r = await authFetch(url, init);
  if (!r.ok) throw new Error(msg);
  return (await r.json()) as T;
}

const JSON_POST: RequestInit = {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: "{}",
};

export async function listBrowserProfiles(): Promise<BrowserProfile[]> {
  const raw = await json<RawProfile[]>(
    "/api/v1/browser-profiles",
    "Failed to load profiles",
  );
  return raw.map(toProfile);
}

export async function createBrowserProfile(name: string): Promise<BrowserProfile> {
  return toProfile(
    await json<RawProfile>(
      "/api/v1/browser-profiles",
      "Failed to create profile",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      },
    ),
  );
}

export async function deleteBrowserProfile(id: string): Promise<void> {
  const r = await authFetch(`/api/v1/browser-profiles/${id}`, {
    method: "DELETE",
  });
  if (!r.ok && r.status !== 204) throw new Error("Failed to delete profile");
}

export async function createSetupSession(
  id: string,
): Promise<{ sessionId: string; expiresAt: string }> {
  const raw = await json<{ session_id: string; expires_at: string }>(
    `/api/v1/browser-profiles/${id}/setup-session`,
    "Failed to start setup",
    JSON_POST,
  );
  return { sessionId: raw.session_id, expiresAt: raw.expires_at };
}

export async function captureProfile(
  id: string,
  sessionId: string,
): Promise<BrowserProfile> {
  return toProfile(
    await json<RawProfile>(
      `/api/v1/browser-profiles/${id}/capture?session_id=${encodeURIComponent(sessionId)}`,
      "Failed to save authentication",
      JSON_POST,
    ),
  );
}
