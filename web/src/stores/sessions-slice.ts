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
  removeSession: (sessionId: string) => void;
  setActiveSession: (sessionId: string | null) => void;
  upsertSession: (session: Session) => void;
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
  // True until the first ``fetchSessions`` settles, so empty-state UI and
  // the route-state guard don't fire before the first list arrives.
  sessionsLoading: true,

  fetchSessions: async (params) => {
    try {
      set({ sessionsLoading: true });
      const res = await sessionsApi.listSessions(params);
      set({
        sessions: res.sessions,
        sessionsTotal: res.total,
        sessionsLoading: false,
      });
    } catch (e) {
      set({ sessionsLoading: false, error: (e as Error).message });
    }
  },

  createSession: async (body) => {
    try {
      const session = await sessionsApi.createSession(body);
      get().upsertSession(session);
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

  removeSession: (sessionId) =>
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== sessionId),
      sessionsTotal: Math.max(0, state.sessionsTotal - 1),
      activeSessionId:
        state.activeSessionId === sessionId ? null : state.activeSessionId,
    })),

  setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),

  upsertSession: (session) =>
    set((state) => {
      const existed = state.sessions.some(
        (candidate) => candidate.id === session.id,
      );
      return {
        activeSessionId: session.id,
        sessions: [
          session,
          ...state.sessions.filter((candidate) => candidate.id !== session.id),
        ],
        sessionsTotal: Math.max(
          state.sessionsTotal,
          state.sessions.length + (existed ? 0 : 1),
        ),
      };
    }),
});
