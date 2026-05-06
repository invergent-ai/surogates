export type AgentChatRole = "user" | "assistant" | "system";

export type AgentChatMessageStatus = "complete" | "streaming" | "error";

export type AgentChatSystemKind = "skill_invoked" | "artifact" | "error";

export interface AgentChatMessage {
  id: string;
  role: AgentChatRole;
  content: string;
  createdAt: Date;
  status: AgentChatMessageStatus;
  toolCalls?: AgentChatToolCallInfo[];
  reasoning?: string;
  systemKind?: AgentChatSystemKind;
  systemMeta?: Record<string, unknown>;
  errorInfo?: AgentChatErrorInfo;
}

export interface AgentChatToolCallInfo {
  id: string;
  toolName: string;
  args: string;
  result?: string;
  status: "running" | "complete" | "error";
  checkpointHash?: string;
  expertResultEventId?: number;
  expertFeedback?: { rating: "up" | "down"; reason?: string };
  clarifyAnswers?: AgentChatClarifyAnswer[];
  cancelled?: boolean;
}

export interface AgentChatTokenUsage {
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
  cachedInputTokens: number;
  totalTokens: number;
  contextWindow: number;
  model: string;
}

export interface AgentChatRetryIndicator {
  title: string;
  detail: string;
  attempt: number;
}

export type AgentChatErrorCategory =
  | "provider_error"
  | "rate_limit"
  | "auth_failed"
  | "context_overflow"
  | "network"
  | "timeout"
  | "invalid_response"
  | "tool_error"
  | "storage_error"
  | "database_error"
  | "governance_denied"
  | "unknown";

export interface AgentChatErrorInfo {
  category: AgentChatErrorCategory;
  title: string;
  detail: string;
  retryable: boolean;
}

export interface AgentChatSession {
  id: string;
  userId?: string | null;
  orgId?: string | null;
  agentId?: string | null;
  channel?: string | null;
  status: "active" | "paused" | "completed" | "failed" | string;
  title?: string | null;
  model?: string | null;
  config?: Record<string, unknown>;
  parentId?: string | null;
  messageCount?: number;
  toolCallCount?: number;
  inputTokens?: number;
  outputTokens?: number;
  estimatedCostUsd?: number | string;
  createdAt?: string;
  updatedAt?: string;
}

export interface AgentChatSessionList {
  sessions: AgentChatSession[];
  total: number;
}

export type AgentChatEventType =
  | "user.message"
  | "llm.request"
  | "llm.response"
  | "llm.thinking"
  | "llm.delta"
  | "tool.call"
  | "tool.result"
  | "session.start"
  | "session.pause"
  | "session.resume"
  | "session.complete"
  | "session.fail"
  | "session.done"
  | "harness.wake"
  | "harness.crash"
  | "context.compact"
  | "skill.invoked"
  | "policy.denied"
  | "stream.timeout"
  | "expert.result"
  | "expert.endorse"
  | "expert.override"
  | "artifact.created"
  | "artifact.updated"
  | "clarify.response";

export interface AgentChatRuntimeEvent {
  type: AgentChatEventType;
  eventId: number;
  data: Record<string, unknown>;
}

export interface AgentChatState {
  messages: AgentChatMessage[];
  isRunning: boolean;
  tokenUsage: AgentChatTokenUsage;
  retryIndicator: AgentChatRetryIndicator | null;
  lastEventId: number;
  sessionDone: boolean;
  hadDeltas: boolean;
  terminal: boolean;
}

export type AgentChatArtifactKind =
  | "markdown"
  | "table"
  | "chart"
  | "html"
  | "svg";

export interface AgentChatArtifactMeta {
  artifact_id: string;
  session_id: string;
  name: string;
  kind: AgentChatArtifactKind;
  version: number;
  size: number;
  created_at: string;
}

export interface AgentChatMarkdownArtifactSpec {
  content: string;
}

export interface AgentChatTableArtifactSpec {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  caption?: string | null;
}

export interface AgentChatChartArtifactSpec {
  vega_lite: Record<string, unknown>;
  caption?: string | null;
}

export interface AgentChatHtmlArtifactSpec {
  html: string;
  caption?: string | null;
}

export interface AgentChatSvgArtifactSpec {
  svg: string;
  caption?: string | null;
}

export type AgentChatArtifactPayload =
  | {
      meta: AgentChatArtifactMeta;
      kind: "markdown";
      spec: AgentChatMarkdownArtifactSpec;
    }
  | { meta: AgentChatArtifactMeta; kind: "table"; spec: AgentChatTableArtifactSpec }
  | { meta: AgentChatArtifactMeta; kind: "chart"; spec: AgentChatChartArtifactSpec }
  | { meta: AgentChatArtifactMeta; kind: "html"; spec: AgentChatHtmlArtifactSpec }
  | { meta: AgentChatArtifactMeta; kind: "svg"; spec: AgentChatSvgArtifactSpec };

export interface AgentChatClarifyChoice {
  label: string;
  description?: string;
}

export interface AgentChatClarifyQuestion {
  prompt: string;
  choices?: AgentChatClarifyChoice[];
  allow_other?: boolean;
}

export interface AgentChatClarifyArgs {
  questions: AgentChatClarifyQuestion[];
}

export interface AgentChatClarifyAnswer {
  question: string;
  answer: string;
  is_other: boolean;
}

export interface AgentChatSlashCommand {
  value: string;
  label: string;
  description: string;
}

export interface AgentChatWorkspaceEntry {
  name: string;
  path: string;
  kind: "file" | "dir";
  size?: number | null;
  children?: AgentChatWorkspaceEntry[] | null;
}

export interface AgentChatWorkspaceTree {
  root: string;
  entries: AgentChatWorkspaceEntry[];
  truncated: boolean;
}

export interface AgentChatWorkspaceFile {
  path: string;
  content: string;
  size: number;
  mime_type?: string | null;
  encoding: "utf-8" | "base64";
  truncated: boolean;
}

export interface AgentChatWorkspaceUpload {
  path: string;
  size: number;
}

export type AgentChatExpertFeedbackRating = "up" | "down";

export interface AgentChatSseMessageEvent {
  data: string;
  lastEventId: string;
}

export interface AgentChatEventStream {
  addEventListener(
    type: AgentChatEventType,
    listener: (event: AgentChatSseMessageEvent) => void,
  ): void;
  close(): void;
  onerror: (() => void) | null;
}

export interface AgentChatAdapter {
  listSessions(input: {
    agentId?: string;
    limit?: number;
    offset?: number;
  }): Promise<AgentChatSessionList>;
  createSession(input: {
    agentId?: string;
    system?: string;
  }): Promise<AgentChatSession>;
  getSession(input: { sessionId: string }): Promise<AgentChatSession>;
  sendMessage(input: {
    sessionId: string;
    content: string;
  }): Promise<{ eventId?: number; status?: string }>;
  pauseSession(input: { sessionId: string }): Promise<void>;
  retrySession(input: { sessionId: string }): Promise<AgentChatSession>;
  deleteSession?(input: { sessionId: string }): Promise<void>;
  getArtifact(input: {
    sessionId: string;
    artifactId: string;
  }): Promise<AgentChatArtifactPayload>;
  submitClarifyResponse(input: {
    sessionId: string;
    toolCallId: string;
    responses: AgentChatClarifyAnswer[];
  }): Promise<{ eventId?: number }>;
  submitExpertFeedback?(input: {
    sessionId: string;
    expertResultEventId: number;
    rating: AgentChatExpertFeedbackRating;
    reason?: string;
  }): Promise<{ eventId?: number; eventType?: string }>;
  listSlashCommands?(): Promise<AgentChatSlashCommand[]>;
  getWorkspaceTree(input: {
    sessionId: string;
  }): Promise<AgentChatWorkspaceTree>;
  getWorkspaceFile(input: {
    sessionId: string;
    path: string;
  }): Promise<AgentChatWorkspaceFile>;
  uploadWorkspaceFile(input: {
    sessionId: string;
    file: File;
    directory?: string;
  }): Promise<AgentChatWorkspaceUpload>;
  deleteWorkspaceFile(input: {
    sessionId: string;
    path: string;
  }): Promise<void>;
  openEventStream(input: {
    sessionId: string;
    after: number;
  }): AgentChatEventStream;
}

export interface AgentChatRuntimeApi {
  messages: AgentChatMessage[];
  isRunning: boolean;
  tokenUsage: AgentChatTokenUsage;
  retryIndicator: AgentChatRetryIndicator | null;
  send(content: string): Promise<void>;
  stop(): Promise<void>;
  retry(): Promise<void>;
  markSending(content: string): void;
  markSendError(errorText: string): void;
}

export type ChatMessage = AgentChatMessage;
export type ToolCallInfo = AgentChatToolCallInfo;
export type TokenUsage = AgentChatTokenUsage;
export type RetryIndicator = AgentChatRetryIndicator;
export type ErrorInfo = AgentChatErrorInfo;
export type ArtifactKind = AgentChatArtifactKind;
export type ArtifactPayload = AgentChatArtifactPayload;
export type MarkdownArtifactSpec = AgentChatMarkdownArtifactSpec;
export type TableArtifactSpec = AgentChatTableArtifactSpec;
export type ChartArtifactSpec = AgentChatChartArtifactSpec;
export type HtmlArtifactSpec = AgentChatHtmlArtifactSpec;
export type SvgArtifactSpec = AgentChatSvgArtifactSpec;
export type ClarifyChoice = AgentChatClarifyChoice;
export type ClarifyQuestion = AgentChatClarifyQuestion;
export type ClarifyArgs = AgentChatClarifyArgs;
export type ClarifyAnswer = AgentChatClarifyAnswer;
export type WorkspaceEntry = AgentChatWorkspaceEntry;
export type WorkspaceTree = AgentChatWorkspaceTree;
export type WorkspaceFile = AgentChatWorkspaceFile;
export type WorkspaceUpload = AgentChatWorkspaceUpload;
