// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type { StateCreator } from "zustand";
import type { AppState } from "./app-store";
import { fetchAuthConfig } from "@/api/auth";

// Per-agent capability state for the standalone web app. The agent is
// resolved server-side (Host header / ?agent_id=), so the client just
// asks ``/auth/config`` which echoes the enabled built-in slash commands.
//
// ``slashCommands === null`` means "not loaded / unknown" — consumers
// fail OPEN (treat every command as enabled) so a fetch hiccup or an
// older backend never bricks the menu. A resolved array (always includes
// "clear") is the authoritative enabled set.
export type CapabilitiesSlice = {
  slashCommands: string[] | null;
  agentId: string | null;

  fetchCapabilities: () => Promise<void>;
};

export const createCapabilitiesSlice: StateCreator<
  AppState,
  [],
  [],
  CapabilitiesSlice
> = (set) => ({
  slashCommands: null,
  agentId: null,

  fetchCapabilities: async () => {
    // ``fetchAuthConfig`` already degrades to a safe fallback on error
    // (no ``slash_commands`` / ``agent_id`` fields), which map to ``null``.
    const config = await fetchAuthConfig();
    set({ slashCommands: config.slash_commands ?? null, agentId: config.agent_id ?? null });
  },
});

// True when slash command *id* should be surfaced for this agent.  Unknown
// (``null``) fails open.  ``ids`` are canonical/hyphenated (e.g. "loop",
// "deep-research").
export function slashCommandEnabled(
  slashCommands: string[] | null,
  id: string,
): boolean {
  return slashCommands === null || slashCommands.includes(id);
}
