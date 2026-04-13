// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { redirect } from "@tanstack/react-router";
import {
  getPostAuthRoute,
  hasAuthToken,
  hasRefreshToken,
  setPostAuthRedirect,
} from "@/features/auth";
import { refreshSession } from "@/api/auth";

async function hasActiveSession(): Promise<boolean> {
  if (hasAuthToken()) return true;
  if (!hasRefreshToken()) return false;
  return refreshSession();
}

export async function requireAuth(): Promise<void> {
  if (!(await hasActiveSession())) {
    const current = window.location.pathname + window.location.search;
    if (current !== "/login") {
      setPostAuthRedirect(current);
    }
    throw redirect({ to: "/login" });
  }
}

export async function requireGuest(): Promise<void> {
  if (!(await hasActiveSession())) return;
  throw redirect({ to: getPostAuthRoute() });
}
