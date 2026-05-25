// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import {
  clearAuthTokens,
  getAuthToken,
  getRefreshToken,
  storeAuthTokens,
} from "@/features/auth";

type RefreshResponse = {
  access_token: string;
  token_type: string;
};

let isRedirecting = false;

function redirectToAuth(): void {
  if (isRedirecting) return;
  isRedirecting = true;
  window.location.href = "/login";
}

export async function refreshSession(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  try {
    const response = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    if (!response.ok) {
      clearAuthTokens();
      return false;
    }

    const payload = (await response.json()) as RefreshResponse;
    // Backend only rotates the access token; refresh token stays the same
    storeAuthTokens(payload.access_token, refreshToken);
    return true;
  } catch {
    return false;
  }
}

export async function authFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  const accessToken = getAuthToken();
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }

  let response: Response;
  try {
    response = await fetch(input, { ...init, headers });
  } catch (err) {
    if (err instanceof TypeError) {
      throw new Error("API server is not reachable.");
    }
    throw err;
  }

  if (response.status !== 401) return response;

  const refreshed = await refreshSession();
  if (!refreshed) {
    clearAuthTokens();
    redirectToAuth();
    return response;
  }

  const retryHeaders = new Headers(init?.headers);
  const newToken = getAuthToken();
  if (newToken) {
    retryHeaders.set("Authorization", `Bearer ${newToken}`);
  } else {
    clearAuthTokens();
  }

  return fetch(input, { ...init, headers: retryHeaders });
}

export async function fetchCurrentUser(): Promise<{
  id: string;
  org_id: string;
  email: string;
  display_name: string | null;
  auth_provider: string;
  created_at: string;
}> {
  const response = await authFetch("/api/v1/auth/me");
  if (!response.ok) throw new Error("Failed to fetch user profile");
  return response.json();
}

export async function updateCurrentUser(fields: {
  display_name?: string;
  email?: string;
}): Promise<{
  id: string;
  org_id: string;
  email: string;
  display_name: string | null;
  auth_provider: string;
  created_at: string;
}> {
  const response = await authFetch("/api/v1/auth/me", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail ?? "Failed to update profile.");
  }
  return response.json();
}

export interface ChannelIdentity {
  id: string;
  platform: string;
  platform_user_id: string;
  platform_meta: Record<string, unknown>;
}

export async function fetchMyChannels(): Promise<ChannelIdentity[]> {
  const response = await authFetch("/api/v1/auth/me/channels");
  if (!response.ok) throw new Error("Failed to fetch channel identities.");
  return response.json();
}

export async function unlinkChannel(identityId: string): Promise<void> {
  const response = await authFetch(`/api/v1/auth/me/channels/${identityId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail ?? "Failed to unlink channel.");
  }
}

// ── BYO Firebase self-registration ──────────────────────────────────────

export type FirebaseProvider = "google" | "github" | "password";

export interface FirebaseRuntimeConfig {
  api_key: string;
  auth_domain: string;
  project_id: string;
  app_id: string | null;
  messaging_sender_id: string | null;
  measurement_id: string | null;
  enabled_providers: FirebaseProvider[];
}

export interface AuthConfigResponse {
  self_registration_enabled: boolean;
  firebase: FirebaseRuntimeConfig | null;
}

/** Fetch the runtime auth config. Falls back to "disabled" on any error
 * so the login page degrades gracefully to local-only login. */
export async function fetchAuthConfig(): Promise<AuthConfigResponse> {
  try {
    const response = await fetch("/api/v1/auth/config");
    if (!response.ok) {
      return { self_registration_enabled: false, firebase: null };
    }
    return (await response.json()) as AuthConfigResponse;
  } catch {
    return { self_registration_enabled: false, firebase: null };
  }
}

/** Exchange a Firebase ID token for Surogates access + refresh tokens. */
export async function exchangeFirebaseToken(idToken: string): Promise<{
  access_token: string;
  refresh_token: string;
  token_type: string;
}> {
  const response = await fetch("/api/v1/auth/firebase/exchange", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: idToken }),
  });
  if (!response.ok) {
    const data = (await response.json().catch(() => null)) as
      | { detail?: string }
      | null;
    throw new Error(data?.detail ?? "Firebase sign-in failed.");
  }
  return (await response.json()) as {
    access_token: string;
    refresh_token: string;
    token_type: string;
  };
}

export function logout(): void {
  clearAuthTokens();
}
