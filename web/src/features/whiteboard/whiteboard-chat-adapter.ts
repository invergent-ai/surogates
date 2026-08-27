import { surogatesWebChatAdapter } from "@/features/chat/surogates-web-chat-adapter";
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type { AgentChatAdapter } from "@invergent/agent-chat-react";

/**
 * The web adapter with every new session stamped as a whiteboard surface.
 *
 * The stamp has to happen at creation: the harness reads
 * ``config.surface`` at wake to pick the tool set and the guidance
 * fragment, and session config is not editable afterwards.
 */
export const whiteboardChatAdapter: AgentChatAdapter = {
  ...surogatesWebChatAdapter,
  createSession(input) {
    return surogatesWebChatAdapter.createSession({
      ...input,
      surface: "whiteboard",
    } as Parameters<AgentChatAdapter["createSession"]>[0]);
  },
};
