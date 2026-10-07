// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The user's computers at this agent, those running Surogate Desktop that can work on their
// folders, and the desktop's sign-ins.

import { signInsFrom } from "@/lib/devices";

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
  reauthorized_at: string | null;
  online: boolean;
}

/** A sign-in of Surogate Desktop, with the computer it added, if any. */
export interface SignIn {
  id: string;
  created_at: string;
  last_used_at: string;
  device_id: string | null;
  device_name: string | null;
}

export async function listDevices(): Promise<Device[]> {
  const response = await authFetch("/api/v1/devices");
  if (!response.ok) {
    return parseError(response, "Failed to list your computers");
  }
  return (await response.json()) as Device[];
}

/** The computer's credential ends, and its queued work is cancelled; it works on no folder until restored there. */
export async function revokeDevice(deviceId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/devices/${encodeURIComponent(deviceId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    return parseError(response, "Failed to revoke this computer");
  }
}

export async function listSignIns(): Promise<SignIn[] | "sign-in-again"> {
  return signInsFrom(await authFetch("/api/v1/auth/oauth/sign-ins"));
}

export async function endSignIn(signInId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/auth/oauth/sign-ins/${encodeURIComponent(signInId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    return parseError(response, "Failed to end this sign-in");
  }
}
