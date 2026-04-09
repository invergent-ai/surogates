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

export function logout(): void {
  clearAuthTokens();
}
