// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatSession,
  AgentChatSlashCommand,
  AgentChatSseMessageEvent,
} from "@invergent/agent-chat-react";
import { getArtifact } from "@/api/artifacts";
import { submitClarifyResponse as submitClarifyResponseApi } from "@/api/clarify";
import { submitExpertFeedback as submitExpertFeedbackApi } from "@/api/feedback";
import { listSkills, type SkillSummary } from "@/api/skills";
import * as sessionsApi from "@/api/sessions";
import { getAuthToken } from "@/features/auth";
import type { Session } from "@/types/session";

export const surogatesWebChatAdapter: AgentChatAdapter = {
  async listSessions(input) {
    const response = await sessionsApi.listSessions({
      limit: input.limit,
      offset: input.offset,
    });
    return {
      sessions: response.sessions.map(toAgentChatSession),
      total: response.total,
    };
  },

  async createSession(input) {
    return toAgentChatSession(
      await sessionsApi.createSession({ system: input.system }),
    );
  },

  async getSession(input) {
    return toAgentChatSession(await sessionsApi.getSession(input.sessionId));
  },

  async sendMessage(input) {
    const response = await sessionsApi.sendMessage(
      input.sessionId,
      input.content,
    );
    return { eventId: response.event_id, status: response.status };
  },

  async pauseSession(input) {
    await sessionsApi.pauseSession(input.sessionId);
  },

  async retrySession(input) {
    return toAgentChatSession(await sessionsApi.retrySession(input.sessionId));
  },

  async deleteSession(input) {
    await sessionsApi.deleteSession(input.sessionId);
  },

  async getArtifact(input) {
    return await getArtifact(input.sessionId, input.artifactId);
  },

  async submitClarifyResponse(input) {
    const response = await submitClarifyResponseApi(
      input.sessionId,
      input.toolCallId,
      input.responses,
    );
    return { eventId: response.event_id };
  },

  async submitExpertFeedback(input) {
    const response = await submitExpertFeedbackApi(
      input.sessionId,
      input.expertResultEventId,
      input.rating,
      input.reason,
    );
    return { eventId: response.event_id, eventType: response.event_type };
  },

  async listSlashCommands() {
    const response = await listSkills();
    return response.skills.map(skillToSlashCommand);
  },

  openEventStream(input) {
    const token = getAuthToken();
    const url = new URL(
      `/api/v1/sessions/${input.sessionId}/events`,
      window.location.origin,
    );
    url.searchParams.set("after", String(input.after));
    if (token) url.searchParams.set("token", token);
    return wrapEventSource(new EventSource(url.toString()));
  },
};

function skillToSlashCommand(skill: SkillSummary): AgentChatSlashCommand {
  const trigger = skill.trigger
    ? skill.trigger.startsWith("/") ? skill.trigger : `/${skill.trigger}`
    : `/${skill.name}`;
  return {
    value: trigger,
    label: trigger,
    description: skill.description,
  };
}

function toAgentChatSession(session: Session): AgentChatSession {
  return {
    id: session.id,
    userId: session.user_id,
    orgId: session.org_id,
    channel: session.channel,
    status: session.status,
    title: session.title,
    model: session.model,
    config: session.config,
    parentId: session.parent_id,
    messageCount: session.message_count,
    toolCallCount: session.tool_call_count,
    inputTokens: session.input_tokens,
    outputTokens: session.output_tokens,
    estimatedCostUsd: session.estimated_cost_usd,
    createdAt: session.created_at,
    updatedAt: session.updated_at,
  };
}

function wrapEventSource(source: EventSource): AgentChatEventStream {
  let errorHandler: (() => void) | null = null;
  return {
    addEventListener(
      type: AgentChatEventType,
      listener: (event: AgentChatSseMessageEvent) => void,
    ) {
      source.addEventListener(type, (event) => {
        const message = event as MessageEvent<string>;
        listener({
          data: message.data,
          lastEventId: message.lastEventId,
        });
      });
    },
    close() {
      source.close();
    },
    get onerror() {
      return errorHandler;
    },
    set onerror(handler: (() => void) | null) {
      errorHandler = handler;
      source.onerror = handler ? () => handler() : null;
    },
  };
}
