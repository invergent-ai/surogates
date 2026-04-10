// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//

export interface TransparencyConfig {
  enabled: boolean;
  level?: "none" | "basic" | "enhanced" | "full";
  require_confirmation?: boolean;
  emotion_recognition?: boolean;
}

let _cached: TransparencyConfig | null = null;

export async function getTransparencyConfig(): Promise<TransparencyConfig> {
  if (_cached) return _cached;
  try {
    const response = await fetch("/api/v1/transparency");
    if (!response.ok) return { enabled: false };
    _cached = (await response.json()) as TransparencyConfig;
    return _cached;
  } catch {
    return { enabled: false };
  }
}
