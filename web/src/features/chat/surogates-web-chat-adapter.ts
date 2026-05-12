// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatInboxEventStream,
  AgentChatInboxStreamEvent,
  AgentChatScheduledWorkItem,
  AgentChatSession,
  AgentChatSlashCommand,
  AgentChatSseMessageEvent,
} from "@invergent/agent-chat-react";
import { getArtifact } from "@/api/artifacts";
import { submitClarifyResponse as submitClarifyResponseApi } from "@/api/clarify";
import { submitExpertFeedback as submitExpertFeedbackApi } from "@/api/feedback";
import * as inboxApi from "@/api/inbox";
import { listSkills, type SkillSummary } from "@/api/skills";
import * as sessionsApi from "@/api/sessions";
import * as workspaceApi from "@/api/workspace";
import { getAuthToken } from "@/features/auth";
import type { ScheduledWorkItem, Session } from "@/types/session";

const DEFAULT_OUTCOME_RUBRIC =
  "The outcome is satisfied only when the assistant's latest response " +
  "explicitly confirms the requested work is complete, clearly presents " +
  "the final deliverable, or clearly explains that the work is blocked or " +
  "unachievable and what remains outside the agent's control.";

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
    const images = input.images?.length
      ? input.images.map((img) => ({
          data: img.data,
          mime_type: img.mimeType ?? "image/png",
        }))
      : undefined;
    const response = await sessionsApi.sendMessage(
      input.sessionId,
      input.content,
      images,
    );
    return { eventId: response.event_id, status: response.status };
  },

  async defineOutcome(input) {
    const response = await sessionsApi.defineOutcome(input.sessionId, {
      description: input.description,
      rubric: input.rubric?.trim() || DEFAULT_OUTCOME_RUBRIC,
      maxIterations: input.maxIterations,
    });
    const event = response.events[0];
    return {
      eventId: event?.event_id,
      outcomeId: event?.outcome_id,
      processedAt: event?.processed_at,
    };
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

  async getSessionTree(input) {
    const response = await sessionsApi.getSessionTree(input.sessionId);
    return {
      total: response.total,
      nodes: response.nodes.map((node) => ({
        id: node.id,
        parentId: node.parent_id,
        rootSessionId: node.root_session_id,
        depth: node.depth,
        agentId: node.agent_id,
        agentType: node.agent_type,
        runKind: node.run_kind,
        channel: node.channel,
        status: node.status,
        title: node.title,
        model: node.model,
        messageCount: node.message_count,
        toolCallCount: node.tool_call_count,
        createdAt: node.created_at,
        updatedAt: node.updated_at,
      })),
    };
  },

  async listScheduledWork(input) {
    const response = await sessionsApi.listScheduledWork({
      status: input.status,
      limit: input.limit,
      offset: input.offset,
    });
    return {
      total: response.total,
      items: response.items.map(toAgentChatScheduledWorkItem),
    };
  },

  async runScheduledWorkNow(input) {
    await sessionsApi.runScheduledWorkNow(input.scheduleId);
  },

  async cancelScheduledWork(input) {
    await sessionsApi.cancelScheduledWork(input.scheduleId);
  },

  async listInbox(input) {
    return await inboxApi.listInbox(input);
  },

  async getInboxItem(input) {
    return await inboxApi.getInboxItem(input.itemId);
  },

  async markInboxItemRead(input) {
    return await inboxApi.markInboxItemRead(input.itemId);
  },

  async acknowledgeInboxItem(input) {
    return await inboxApi.acknowledgeInboxItem(input.itemId);
  },

  async deleteInboxItem(input) {
    await inboxApi.deleteInboxItem(input.itemId);
  },

  async respondGovernanceInboxItem(input) {
    return await inboxApi.respondGovernanceInboxItem(
      input.itemId,
      input.decision,
    );
  },

  async respondActionRequiredInboxItem(input) {
    return await inboxApi.respondActionRequiredInboxItem(input.itemId);
  },

  openInboxStream() {
    const token = getAuthToken();
    const url = new URL("/api/v1/inbox/stream", window.location.origin);
    if (token) url.searchParams.set("token", token);
    return wrapInboxEventSource(new EventSource(url.toString()));
  },

  async stopSession(input) {
    await sessionsApi.stopSession(input.sessionId);
  },

  async getBrowserState(sessionId) {
    const state = await sessionsApi.getBrowserState(sessionId);
    if (state === null) return null;
    return {
      status: state.status,
      controlOwner: state.control_owner,
      liveViewPath: state.live_view_path,
    };
  },

  async getBrowserPreviewSnapshot(sessionId) {
    const blob = await sessionsApi.getBrowserPreviewSnapshot(sessionId);
    return {
      src: await blobToDataUrl(blob),
      capturedAt: new Date().toISOString(),
    };
  },

  async acquireBrowserControl(sessionId) {
    const response = await sessionsApi.acquireBrowserControl(sessionId);
    return {
      outcome: response.outcome,
      ownerUserId: response.owner_user_id,
    };
  },

  async releaseBrowserControl(sessionId) {
    await sessionsApi.releaseBrowserControl(sessionId);
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

  async getWorkspaceTree(input) {
    return await workspaceApi.getWorkspaceTree(input.sessionId);
  },

  async getWorkspaceFile(input) {
    return await workspaceApi.getWorkspaceFile(input.sessionId, input.path);
  },

  async uploadWorkspaceFile(input) {
    return await workspaceApi.uploadFile(
      input.sessionId,
      input.file,
      input.directory,
    );
  },

  async deleteWorkspaceFile(input) {
    await workspaceApi.deleteFile(input.sessionId, input.path);
  },

  getWorkspaceDownloadUrl(input) {
    // The download endpoint accepts the JWT via ``?token=`` (same flow
    // SSE uses) — anchor downloads can't carry the Authorization header.
    const url = new URL(
      workspaceApi.getDownloadUrl(input.sessionId, input.path),
      window.location.origin,
    );
    const token = getAuthToken();
    if (token) url.searchParams.set("token", token);
    // Return a relative URL so the browser stays on-origin.
    return url.pathname + url.search;
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

  browserLiveViewUrl(sessionId) {
    const url = new URL(
      `/api/v1/sessions/${sessionId}/browser/live/`,
      window.location.origin,
    );
    const token = getAuthToken();
    if (token) url.searchParams.set("token", token);
    url.searchParams.set("pwd", "admin");
    return url.pathname + url.search;
  },
};

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("Failed to read blob"));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(blob);
  });
}

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

export function toAgentChatSession(session: Session): AgentChatSession {
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
    runKind: deriveRunKind(session.channel, session.config),
    messageCount: session.message_count,
    toolCallCount: session.tool_call_count,
    inputTokens: session.input_tokens,
    outputTokens: session.output_tokens,
    estimatedCostUsd: session.estimated_cost_usd,
    createdAt: session.created_at,
    updatedAt: session.updated_at,
  };
}

function toAgentChatScheduledWorkItem(
  item: ScheduledWorkItem,
): AgentChatScheduledWorkItem {
  return {
    id: item.id,
    agentId: item.agent_id,
    name: item.name,
    prompt: item.prompt,
    status: item.status,
    kind: item.kind,
    source: item.source,
    scheduleDisplay: item.schedule_display,
    timezone: item.timezone,
    runCount: item.run_count,
    repeatLimit: item.repeat_limit,
    nextRunAt: item.next_run_at,
    lastRunAt: item.last_run_at,
    lastSessionId: item.last_session_id,
    lastError: item.last_error,
    expiresAt: item.expires_at,
    createdFromSessionId: item.created_from_session_id,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
  };
}

function deriveRunKind(
  channel: string | null | undefined,
  config: Record<string, unknown> | null | undefined,
): string | null {
  if (channel === "scheduled" && config?.scheduled_dynamic_loop === true) {
    return "dynamic_loop";
  }
  if (channel === "scheduled") return "scheduled";
  return null;
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

function wrapInboxEventSource(source: EventSource): AgentChatInboxEventStream {
  let errorHandler: (() => void) | null = null;
  return {
    addEventListener(
      type: "item" | "snapshot",
      listener: (event: AgentChatInboxStreamEvent) => void,
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
