// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The parts of an adapter the inbox never touches. `AgentChatAdapter`
// requires them all, and each inbox test file was declaring its own
// identical set just to satisfy the type.

import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import type {
  AgentChatAdapter,
  AgentChatArtifactPayload,
  AgentChatSession,
  AgentChatSessionList,
  AgentChatWorkspaceFile,
  AgentChatWorkspaceTree,
  AgentChatWorkspaceUpload,
} from "../src/types";

export function session(
  input: Partial<AgentChatSession> & { id: string },
): AgentChatSession {
  return {
    status: "completed",
    title: "Session",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    ...input,
  };
}

export const NON_INBOX_ADAPTER: AgentChatAdapter = {
  ...NO_BROWSER_ADAPTER,
  async listSessions(): Promise<AgentChatSessionList> {
    return { sessions: [], total: 0 };
  },
  async createSession() {
    return session({ id: "created" });
  },
  async getSession(input) {
    return session({ id: input.sessionId });
  },
  async sendMessage() {
    return { eventId: 1, status: "accepted" };
  },
  async pauseSession() {},
  async retrySession(input) {
    return session({ id: input.sessionId });
  },
  async getArtifact(): Promise<AgentChatArtifactPayload> {
    throw new Error("not used by inbox tests");
  },
  async submitAskUserQuestionResponse() {
    return { eventId: 1 };
  },
  async getWorkspaceTree(): Promise<AgentChatWorkspaceTree> {
    return { root: "workspace", entries: [], truncated: false };
  },
  async getWorkspaceFile(): Promise<AgentChatWorkspaceFile> {
    throw new Error("not used by inbox tests");
  },
  async uploadWorkspaceFile(): Promise<AgentChatWorkspaceUpload> {
    return { path: "uploaded.txt", size: 4 };
  },
  async deleteWorkspaceFile() {},
  getWorkspaceDownloadUrl(input) {
    return `/api/v1/sessions/${input.sessionId}/workspace/download?path=${encodeURIComponent(input.path)}`;
  },
  openEventStream() {
    throw new Error("not used by inbox tests");
  },
};
