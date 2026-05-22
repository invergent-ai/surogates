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

/**
 * A workspace-resident attachment referenced from a user message.
 *
 * ``path`` is the workspace-relative location returned by
 * ``adapter.uploadWorkspaceFile``.  The harness validates this against the
 * session's workspace bucket before persisting it on the user.message event.
 */
export interface AgentChatAttachment {
  /** Workspace-relative path returned by uploadWorkspaceFile, e.g.
   *  ``"uploads/1715-0-report.pdf"``. */
  path: string;
  /** Original filename for display (the workspace path is the SDK's
   *  collision-avoidance rename). */
  filename: string;
  mimeType?: string;
  size?: number;
}

/**
 * Display variant used on user-message bubbles.
 *
 * ``path`` is optional: an optimistic local message rendered before the
 * upload completes has the filename + MIME but no workspace path yet, so
 * the chip renders disabled (not clickable) until the persisted event
 * replaces it with a full :class:`AgentChatAttachment`.
 */
export interface AgentChatDisplayAttachment {
  path?: string;
  filename: string;
  mimeType?: string;
  size?: number;
}

/**
 * Internal-to-runtime variant carrying the raw ``File`` so the runtime
 * can upload it to the workspace.  Never crosses the network — only
 * passed from composer to runtime.
 */
export interface AgentChatPendingAttachment extends AgentChatDisplayAttachment {
  file: File;
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
  attachments?: AgentChatDisplayAttachment[];
  llmResponseEventId?: number;
  userFeedback?: { rating: "up" | "down"; reason?: string };
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

// ---------------------------------------------------------------------------
// Missions — orchestrated, rubric-judged objectives
//
// Wire format mirrors the backend `/v1/missions/*` REST surface; see
// `surogates/api/routes/missions.py` and
// `docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md`.
// ---------------------------------------------------------------------------

export type AgentChatMissionStatus =
  | "active"
  | "paused"
  | "satisfied"
  | "blocked"
  | "failed"
  | "cancelled"
  | "max_iterations_reached"
  | (string & {});

export interface AgentChatMissionSummary {
  id: string;
  orgId: string;
  userId: string | null;
  serviceAccountId: string | null;
  sessionId: string;
  agentId: string;
  description: string;
  rubric: string;
  status: AgentChatMissionStatus;
  iteration: number;
  maxIterations: number;
  lastEvaluationResult: string | null;
  lastEvaluationExplanation: string | null;
  lastEvaluationFeedback: string | null;
  lastEvaluationAt: string | null;
  evaluatorParseFailures: number;
  pausedReason: string | null;
  cancelledReason: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface AgentChatMissionTask {
  id: string;
  goal: string;
  status: string;
  attemptCount: number;
  maxAttempts: number;
  agentDefName: string | null;
  result: string | null;
  resultMetadata: Record<string, unknown> | null;
  parentIds: string[];
  currentSessionId: string | null;
  createdAt: string | null;
  completedAt: string | null;
}

export type AgentChatMissionWorkerKind = "task" | "worker" | "delegation";

export interface AgentChatMissionWorker {
  /**
   * Which delegation primitive produced this child:
   *
   * - "task"       — `spawn_task` (durable Task row, retried by dispatcher)
   * - "worker"     — `spawn_worker` (async one-shot, durable session)
   * - "delegation" — `delegate_task` (sync fork-join, ephemeral session)
   *
   * `taskId` / `taskStatus` are non-null only for `kind === "task"` —
   * the other two primitives don't create Task rows.
   */
  kind: AgentChatMissionWorkerKind;
  taskId: string | null;
  workerSessionId: string;
  agentDefName: string | null;
  taskStatus: string | null;
  sessionStatus: string;
  latestEventId: number | null;
  latestEventKind: string | null;
  latestEventAt: string | null;
  latestEventSummary: string | null;
  transcriptUrl: string;
}

export interface AgentChatMissionList {
  missions: AgentChatMissionSummary[];
}

export type AgentChatInboxKind =
  | "input_required"
  | "action_required"
  | "task_complete"
  | "governance_gate"
  | "progress_checkin"
  | (string & {});

export type AgentChatInboxStatus =
  | "pending"
  | "acknowledged"
  | "responded"
  | "expired"
  | (string & {});

export interface AgentChatInboxItem {
  id: number;
  orgId: string;
  userId: string;
  sessionId: string;
  sourceEventId: number;
  kind: AgentChatInboxKind;
  status: AgentChatInboxStatus;
  title: string;
  body: string | null;
  payload: Record<string, unknown>;
  actionRef: Record<string, unknown> | null;
  createdAt: string;
  updatedAt: string;
  readAt: string | null;
  respondedAt: string | null;
}

export interface AgentChatInboxList {
  items: AgentChatInboxItem[];
  nextCursor: string | null;
}

export interface AgentChatInboxListInput {
  status?: AgentChatInboxStatus;
  kind?: AgentChatInboxKind;
  sessionId?: string;
  cursor?: string;
  limit?: number;
}

export interface AgentChatInboxStreamEvent {
  data: string;
  lastEventId?: string;
}

export interface AgentChatInboxEventStream {
  addEventListener(
    type: "item" | "snapshot",
    listener: (event: AgentChatInboxStreamEvent) => void,
  ): void;
  close(): void;
  onerror: (() => void) | null;
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
  | "user.feedback"
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

export interface AgentChatBrowserPreviewSnapshot {
  src: string;
  capturedAt?: string;
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
    metadata?: Record<string, unknown>;
    attachments?: AgentChatAttachment[];
  }): Promise<{ eventId?: number; status?: string }>;
  defineOutcome?(input: {
    sessionId: string;
    description: string;
    rubric?: string;
    maxIterations?: number;
  }): Promise<{ eventId?: number; outcomeId?: string; processedAt?: string }>;
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
  // ---- Missions (orchestrated rubric-judged objectives) ----
  // Optional so adapters that don't implement /v1/missions pass type
  // checks. The dashboard probes for `listMissions` at runtime.
  listMissions?(input?: {
    status?: string;
    agentId?: string;
  }): Promise<AgentChatMissionList>;
  getMission?(input: { missionId: string }): Promise<AgentChatMissionSummary>;
  getMissionTasks?(input: {
    missionId: string;
  }): Promise<{ tasks: AgentChatMissionTask[] }>;
  getMissionWorkers?(input: {
    missionId: string;
  }): Promise<{ workers: AgentChatMissionWorker[] }>;
  pauseMission?(input: {
    missionId: string;
    reason?: string;
  }): Promise<void>;
  resumeMission?(input: { missionId: string }): Promise<void>;
  cancelMission?(input: {
    missionId: string;
    reason?: string;
    cascadeToWorkers?: boolean;
  }): Promise<void>;

  listInbox?(input?: AgentChatInboxListInput): Promise<AgentChatInboxList>;
  getInboxItem?(input: { itemId: number }): Promise<AgentChatInboxItem>;
  markInboxItemRead?(input: { itemId: number }): Promise<AgentChatInboxItem>;
  acknowledgeInboxItem?(input: { itemId: number }): Promise<AgentChatInboxItem>;
  deleteInboxItem?(input: { itemId: number }): Promise<void>;
  respondGovernanceInboxItem?(input: {
    itemId: number;
    decision: "approve" | "reject";
  }): Promise<AgentChatInboxItem>;
  respondActionRequiredInboxItem?(input: {
    itemId: number;
  }): Promise<AgentChatInboxItem>;
  openInboxStream?(): AgentChatInboxEventStream;
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
  submitUserFeedback?(input: {
    sessionId: string;
    llmResponseEventId: number;
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
  getBrowserPreviewSnapshot?(
    sessionId: string,
  ): Promise<AgentChatBrowserPreviewSnapshot | null>;
  acquireBrowserControl(sessionId: string): Promise<AgentChatBrowserControlResponse>;
  releaseBrowserControl(sessionId: string): Promise<void>;
  browserLiveViewUrl(sessionId: string): string;
  /**
   * Permanently terminate the session's browser sandbox (destroys the
   * pod / container, drops the registry entry). Optional — when
   * undefined, the BrowserPane's Close button skips this step and
   * just hides the pane via the local onClose callback.
   */
  closeBrowserSession?(sessionId: string): Promise<void>;
}

export interface AgentChatRuntimeApi {
  state: AgentChatState;
  session: AgentChatSession | null;
  messages: AgentChatMessage[];
  isRunning: boolean;
  isLoadingHistory: boolean;
  tokenUsage: AgentChatTokenUsage;
  retryIndicator: AgentChatRetryIndicator | null;
  workspaceRefreshKey: number;
  send(
    content: string,
    images?: AgentChatImageAttachment[],
    attachments?: AgentChatPendingAttachment[],
  ): Promise<void>;
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
export type InboxItem = AgentChatInboxItem;
export type InboxList = AgentChatInboxList;
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
