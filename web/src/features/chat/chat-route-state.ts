// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
export type ChatRouteStateInput = {
  activeSessionId: string | null;
  sessionIds: string[];
  sessionsLoading: boolean;
  urlSessionId?: string;
};

export type ChatRouteState = {
  sessionId: string | null;
  nextActiveSessionId: string | null;
  redirectTo: "/chat" | null;
};

export function getChatRouteState({
  activeSessionId,
  sessionIds,
  sessionsLoading,
  urlSessionId,
}: ChatRouteStateInput): ChatRouteState {
  if (sessionsLoading) {
    return {
      sessionId: urlSessionId ?? null,
      nextActiveSessionId: activeSessionId,
      redirectTo: null,
    };
  }

  if (!urlSessionId) {
    return {
      sessionId: null,
      nextActiveSessionId: null,
      redirectTo: null,
    };
  }

  if (!sessionIds.includes(urlSessionId)) {
    return {
      sessionId: null,
      nextActiveSessionId: null,
      redirectTo: "/chat",
    };
  }

  return {
    sessionId: urlSessionId,
    nextActiveSessionId: urlSessionId,
    redirectTo: null,
  };
}
