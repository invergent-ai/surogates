// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
export type ChatRouteStateInput = {
  activeSessionId: string | null;
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

  // Sub-agent ids aren't in the top-level list -- trust the URL and let
  // the chat adapter surface invalid ids via ``getSession``.
  return {
    sessionId: urlSessionId,
    nextActiveSessionId: urlSessionId,
    redirectTo: null,
  };
}
