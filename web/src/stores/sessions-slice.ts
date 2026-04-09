// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type { StateCreator } from "zustand";
import type { AppState } from "./app-store";
import type { Session, SessionCreateRequest } from "@/types/session";
import * as sessionsApi from "@/api/sessions";

export type SessionsSlice = {
  sessions: Session[];
  sessionsTotal: number;
  activeSessionId: string | null;
  sessionsLoading: boolean;

  fetchSessions: (params?: {
    limit?: number;
    offset?: number;
  }) => Promise<void>;
  createSession: (body: SessionCreateRequest) => Promise<Session | null>;
  deleteSession: (sessionId: string) => Promise<boolean>;
  setActiveSession: (sessionId: string | null) => void;
};

export const createSessionsSlice: StateCreator<
  AppState,
  [],
  [],
  SessionsSlice
> = (set, get) => ({
  sessions: [],
  sessionsTotal: 0,
  activeSessionId: null,
  sessionsLoading: false,

  fetchSessions: async (params) => {
    try {
      set({ sessionsLoading: true });
      const res = await sessionsApi.listSessions(params);
      set({
        sessions: res.sessions,
        sessionsTotal: res.total,
        sessionsLoading: false,
      });
      const active = get().activeSessionId;
      if (active && !res.sessions.find((s) => s.id === active)) {
        set({ activeSessionId: null });
      }
    } catch (e) {
      set({ sessionsLoading: false, error: (e as Error).message });
    }
  },

  createSession: async (body) => {
    try {
      const session = await sessionsApi.createSession(body);
      set({ activeSessionId: session.id });
      await get().fetchSessions();
      return session;
    } catch (e) {
      set({ error: (e as Error).message });
      return null;
    }
  },

  deleteSession: async (sessionId) => {
    try {
      await sessionsApi.deleteSession(sessionId);
      if (get().activeSessionId === sessionId) {
        set({ activeSessionId: null });
      }
      await get().fetchSessions();
      return true;
    } catch (e) {
      set({ error: (e as Error).message });
      return false;
    }
  },

  setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),
});
