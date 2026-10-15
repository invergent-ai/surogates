// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { create } from "zustand";
import {
  type CapabilitiesSlice,
  createCapabilitiesSlice,
} from "./capabilities-slice";
import { createSessionsSlice, type SessionsSlice } from "./sessions-slice";
import { createUserSlice, type UserSlice } from "./user-slice";
import { createWorkspaceSlice, type WorkspaceSlice } from "./workspace-slice";

export type AppState = SessionsSlice &
  UserSlice &
  WorkspaceSlice &
  CapabilitiesSlice & {
    loading: boolean;
    error: string | null;
    /** The desktop sidebar stays open; unpinned it is a rail that opens on hover. */
    sidebarPinned: boolean;
    setSidebarPinned: (pinned: boolean) => void;
  };

export const useAppStore = create<AppState>((...a) => ({
  loading: false,
  error: null,
  // In the store, not the shell: every page draws its own AppShell, so state
  // held there would reset on each navigation.
  sidebarPinned: true,
  setSidebarPinned: (sidebarPinned) => a[0]({ sidebarPinned }),
  ...createSessionsSlice(...a),
  ...createUserSlice(...a),
  ...createWorkspaceSlice(...a),
  ...createCapabilitiesSlice(...a),
}));
