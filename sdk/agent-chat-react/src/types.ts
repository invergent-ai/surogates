export type AgentChatRole = "user" | "assistant" | "system";

export type AgentChatMessageStatus = "complete" | "streaming" | "error";

export type AgentChatSystemKind =
  | "skill_invoked"
  | "artifact"
  | "error"
  | "browser_marker"
  | "browser_marker_warning";

export interface AgentChatImageAttachment {
  /** data: URL (data:image/png;base64,...) or raw base64 string */
  data: string;
  /** MIME type, e.g. "image/png", "image/jpeg" */
  mimeType?: string;
}

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
  images?: AgentChatImageAttachment[];
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
  runKind?: string | null;
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

export interface AgentChatSessionTreeNode {
  id: string;
  parentId: string | null;
  rootSessionId?: string | null;
  depth?: number;
  agentId?: string | null;
  agentType?: string | null;
  runKind?: string | null;
  channel?: string | null;
  status: "active" | "paused" | "completed" | "failed" | string;
  title?: string | null;
  model?: string | null;
  messageCount?: number;
  toolCallCount?: number;
  createdAt: string;
  updatedAt: string;
}

export interface AgentChatSessionTree {
  nodes: AgentChatSessionTreeNode[];
  total: number;
}

export type AgentChatScheduledWorkKind =
  | "cron"
  | "dynamic_loop"
  | "one_shot"
  | "scheduled"
  | (string & {});

export interface AgentChatScheduledWorkItem {
  id: string;
  agentId?: string | null;
  name?: string | null;
  prompt: string;
  status: "active" | "paused" | "completed" | "failed" | string;
  kind?: AgentChatScheduledWorkKind | null;
  source?: string | null;
  scheduleDisplay: string;
  timezone?: string | null;
  runCount: number;
  repeatLimit?: number | null;
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  lastSessionId?: string | null;
  lastError?: string | null;
  expiresAt?: string | null;
  createdFromSessionId?: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface AgentChatScheduledWorkList {
  items: AgentChatScheduledWorkItem[];
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
  | "browser.provisioned"
  | "browser.destroyed"
  | "browser.control_granted"
  | "browser.control_returned"
  | "clarify.response";

export interface AgentChatRuntimeEvent {
  type: AgentChatEventType;
  eventId: number;
  data: Record<string, unknown>;
}

export interface AgentChatBrowserState {
  status: "provisioning" | "live" | "user-control" | "closed";
  controlOwner: string | null;
}

export interface AgentChatBrowserStateResponse {
  status: "live" | "user-control";
  controlOwner: string | null;
  liveViewPath: string;
}

export interface AgentChatBrowserControlResponse {
  outcome: "granted" | "refreshed" | "conflict";
  ownerUserId: string;
}

export interface AgentChatState {
  messages: AgentChatMessage[];
  isRunning: boolean;
  isLoadingHistory: boolean;
  tokenUsage: AgentChatTokenUsage;
  retryIndicator: AgentChatRetryIndicator | null;
  lastEventId: number;
  sessionDone: boolean;
  hadDeltas: boolean;
  terminal: boolean;
  workspaceRefreshKey: number;
  browser: AgentChatBrowserState | null;
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
  chart_js: Record<string, unknown>;
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
    images?: AgentChatImageAttachment[];
  }): Promise<{ eventId?: number; status?: string }>;
  pauseSession(input: { sessionId: string }): Promise<void>;
  retrySession(input: { sessionId: string }): Promise<AgentChatSession>;
  deleteSession?(input: { sessionId: string }): Promise<void>;
  getSessionTree?(input: { sessionId: string }): Promise<AgentChatSessionTree>;
  stopSession?(input: { sessionId: string }): Promise<void>;
  listScheduledWork?(input: {
    agentId?: string;
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<AgentChatScheduledWorkList>;
  runScheduledWorkNow?(input: {
    scheduleId: string;
  }): Promise<{ sessionId?: string } | void>;
  cancelScheduledWork?(input: { scheduleId: string }): Promise<void>;
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
  /**
   * Build a same-origin URL the browser can navigate to (or anchor at via
   * ``<a href download>``) to download the workspace file.  The adapter
   * is responsible for embedding any auth credential the server expects
   * — typically as a query-string token, since cross-origin/anchor
   * downloads can't carry custom headers.
   */
  getWorkspaceDownloadUrl(input: {
    sessionId: string;
    path: string;
  }): string;
  openEventStream(input: {
    sessionId: string;
    after: number;
  }): AgentChatEventStream;
  getBrowserState(sessionId: string): Promise<AgentChatBrowserStateResponse | null>;
  acquireBrowserControl(sessionId: string): Promise<AgentChatBrowserControlResponse>;
  releaseBrowserControl(sessionId: string): Promise<void>;
  browserLiveViewUrl(sessionId: string): string;
}

export interface AgentChatRuntimeApi {
  session: AgentChatSession | null;
  messages: AgentChatMessage[];
  isRunning: boolean;
  isLoadingHistory: boolean;
  tokenUsage: AgentChatTokenUsage;
  retryIndicator: AgentChatRetryIndicator | null;
  workspaceRefreshKey: number;
  send(content: string, images?: AgentChatImageAttachment[]): Promise<void>;
  stop(): Promise<void>;
  retry(): Promise<void>;
  markSending(content: string): void;
  markSendError(errorText: string): void;
}

export type ChatMessage = AgentChatMessage;
export type SessionTreeNode = AgentChatSessionTreeNode;
export type SessionTree = AgentChatSessionTree;
export type ScheduledWorkItem = AgentChatScheduledWorkItem;
export type ScheduledWorkList = AgentChatScheduledWorkList;
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
