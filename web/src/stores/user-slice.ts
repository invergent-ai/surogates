// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type { StateCreator } from "zustand";
import type { AppState } from "./app-store";
import { fetchCurrentUser } from "@/api/auth";

export interface UserProfile {
  id: string;
  org_id: string;
  email: string;
  display_name: string | null;
  auth_provider: string;
  created_at: string;
}

export type UserSlice = {
  user: UserProfile | null;
  userLoading: boolean;

  fetchUser: () => Promise<void>;
  clearUser: () => void;
};

export const createUserSlice: StateCreator<AppState, [], [], UserSlice> = (
  set,
) => ({
  user: null,
  userLoading: false,

  fetchUser: async () => {
    try {
      set({ userLoading: true });
      const user = await fetchCurrentUser();
      set({ user, userLoading: false });
    } catch (e) {
      set({ userLoading: false, error: (e as Error).message });
    }
  },

  clearUser: () => set({ user: null }),
});
