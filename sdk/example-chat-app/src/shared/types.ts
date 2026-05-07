import type {
  AgentChatArtifactPayload,
  AgentChatClarifyAnswer,
  AgentChatEventType,
  AgentChatExpertFeedbackRating,
  AgentChatRuntimeEvent,
  AgentChatSession,
  AgentChatSessionList,
  AgentChatSlashCommand,
  AgentChatWorkspaceFile,
  AgentChatWorkspaceTree,
  AgentChatWorkspaceUpload,
} from "@invergent/agent-chat-react";

export type ExampleEventType = AgentChatEventType;
export type ExampleRuntimeEvent = AgentChatRuntimeEvent;
export type ExampleSession = AgentChatSession;
export type ExampleSessionList = AgentChatSessionList;
export type ExampleSlashCommand = AgentChatSlashCommand;
export type ExampleArtifactPayload = AgentChatArtifactPayload;
export type ExampleWorkspaceTree = AgentChatWorkspaceTree;
export type ExampleWorkspaceFile = AgentChatWorkspaceFile;
export type ExampleWorkspaceUpload = AgentChatWorkspaceUpload;

export interface SendMessageResponse {
  eventId?: number;
  status: string;
}

export interface ConfigResponse {
  model: string;
  baseUrl: string;
  hasApiKey: boolean;
}

export interface ClarifyResponseRequest {
  responses: AgentChatClarifyAnswer[];
}

export interface ExpertFeedbackRequest {
  expertResultEventId: number;
  rating: AgentChatExpertFeedbackRating;
  reason?: string;
}
