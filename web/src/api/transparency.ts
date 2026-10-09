// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//

export interface TransparencyConfig {
  enabled: boolean;
  level?: "none" | "basic" | "enhanced" | "full";
  // Server-composed disclosure text (per-agent). Newer runtimes send
  // it; when absent the banner falls back to its local copies.
  text?: string;
  // Set where the config could not be read: no banner is drawn, and
  // nothing is sent for the user that they did not type.
  read?: false;
}

const UNREAD: TransparencyConfig = { enabled: false, read: false };

let _cached: TransparencyConfig | null = null;

export async function getTransparencyConfig(): Promise<TransparencyConfig> {
  if (_cached) return _cached;
  try {
    const response = await fetch("/api/v1/transparency");
    if (!response.ok) {
      return UNREAD;
    }
    _cached = (await response.json()) as TransparencyConfig;
    return _cached;
  } catch {
    return UNREAD;
  }
}
