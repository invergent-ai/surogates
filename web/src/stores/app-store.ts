// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { create } from "zustand";
import { createSessionsSlice, type SessionsSlice } from "./sessions-slice";
import { createUserSlice, type UserSlice } from "./user-slice";

export type AppState = SessionsSlice &
  UserSlice & {
    loading: boolean;
    error: string | null;
  };

export const useAppStore = create<AppState>((...a) => ({
  loading: false,
  error: null,
  ...createSessionsSlice(...a),
  ...createUserSlice(...a),
}));
