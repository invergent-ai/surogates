// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The user's computers at this agent: those running Surogate Desktop that can work on their folders.

import { parseError } from "./_errors";
import { authFetch } from "./auth";

/** A computer of the user's, as GET /api/v1/devices lists it. */
export interface Device {
  id: string;
  name: string;
  token_prefix: string;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
  online: boolean;
}

export async function listDevices(): Promise<Device[]> {
  const response = await authFetch("/api/v1/devices");
  if (!response.ok) return parseError(response, "Failed to list your computers");
  return (await response.json()) as Device[];
}
