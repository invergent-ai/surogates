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
  // "Multi session" capability. ``null`` = unknown (fail open — multi on);
  // ``false`` pins each user to one session per channel.
  multiSession: boolean | null;
  // "Live browser support" capability. ``null`` = unknown (fail open —
  // browser affordances shown); ``false`` hides them.
  browserEnabled: boolean | null;
  // Messaging channels an end-user can link their identity to. ``null`` =
  // unknown (not loaded); an array (possibly empty) is authoritative — an
  // empty array hides "Connected Channels".
  linkableChannels: string[] | null;

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
  multiSession: null,
  browserEnabled: null,
  linkableChannels: null,

  fetchCapabilities: async () => {
    // ``fetchAuthConfig`` already degrades to a safe fallback on error
    // (no ``slash_commands`` / ``agent_id`` fields), which map to ``null``.
    const config = await fetchAuthConfig();
    set({
      slashCommands: config.slash_commands ?? null,
      agentId: config.agent_id ?? null,
      multiSession: config.multi_session ?? null,
      browserEnabled: config.browser_enabled ?? null,
      linkableChannels: config.linkable_channels ?? null,
    });
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

// True when the browser-profile affordances (settings tab + composer
// picker) should be surfaced.  Only an explicit ``false`` hides them;
// unknown (``null``) fails open so a fetch hiccup or older backend keeps
// them visible.
export function browserCapabilityEnabled(
  browserEnabled: boolean | null,
): boolean {
  return browserEnabled !== false;
}
