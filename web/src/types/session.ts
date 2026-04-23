// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//

export interface Session {
  id: string;
  user_id: string;
  org_id: string;
  channel: string;
  status: "active" | "paused" | "completed" | "failed";
  title: string | null;
  model: string | null;
  config: Record<string, unknown>;
  parent_id: string | null;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
  created_at: string;
  updated_at: string;
}

export interface SessionCreateRequest {
  system?: string;
}

export interface SessionEvent {
  id: number;
  session_id: string;
  type: EventType;
  data: Record<string, unknown>;
  created_at: string;
}

export type EventType =
  | "user.message"
  | "llm.request"
  | "llm.response"
  | "llm.thinking"
  | "llm.delta"
  | "tool.call"
  | "tool.result"
  | "sandbox.provision"
  | "sandbox.execute"
  | "sandbox.result"
  | "sandbox.destroy"
  | "session.start"
  | "session.pause"
  | "session.resume"
  | "session.complete"
  | "session.fail"
  | "context.compact"
  | "memory.update"
  | "harness.wake"
  | "harness.crash"
  | "worker.spawned"
  | "worker.complete"
  | "worker.failed"
  | "artifact.created"
  | "artifact.updated"
  | "clarify.response";

// ── Clarify tool ─────────────────────────────────────────────────────
//
// Shape of the `clarify` tool's JSON arguments (from tool.call events)
// and the response payload submitted back through the respond endpoint.

export interface ClarifyChoice {
  label: string;
  description?: string;
}

export interface ClarifyQuestion {
  prompt: string;
  choices?: ClarifyChoice[];
  allow_other?: boolean;
}

export interface ClarifyArgs {
  questions: ClarifyQuestion[];
}

export interface ClarifyAnswer {
  question: string;
  answer: string;
  is_other: boolean;
}

export interface ClarifyResponsePayload {
  tool_call_id: string;
  responses: ClarifyAnswer[];
}

// ── Error classification ────────────────────────────────────────────
//
// Structured error info attached to harness.crash and session.fail
// events by the backend classifier (surogates/harness/error_classify.py).
// The UI renders title + collapsible detail, and uses `retryable` to
// gate the Retry button on failed-session error bubbles.

export type ErrorCategory =
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

export interface ErrorInfo {
  category: ErrorCategory;
  title: string;
  detail: string;
  retryable: boolean;
}

// ── Artifacts ───────────────────────────────────────────────────────

export type ArtifactKind =
  | "markdown"
  | "table"
  | "chart"
  | "html"
  | "svg";

export interface ArtifactMeta {
  artifact_id: string;
  session_id: string;
  name: string;
  kind: ArtifactKind;
  version: number;
  size: number;
  created_at: string;
}

export interface MarkdownArtifactSpec {
  content: string;
}

export interface TableArtifactSpec {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  caption?: string | null;
}

export interface ChartArtifactSpec {
  vega_lite: Record<string, unknown>;
  caption?: string | null;
}

export interface HtmlArtifactSpec {
  html: string;
  caption?: string | null;
}

export interface SvgArtifactSpec {
  svg: string;
  caption?: string | null;
}

export type ArtifactPayload =
  | { meta: ArtifactMeta; kind: "markdown"; spec: MarkdownArtifactSpec }
  | { meta: ArtifactMeta; kind: "table"; spec: TableArtifactSpec }
  | { meta: ArtifactMeta; kind: "chart"; spec: ChartArtifactSpec }
  | { meta: ArtifactMeta; kind: "html"; spec: HtmlArtifactSpec }
  | { meta: ArtifactMeta; kind: "svg"; spec: SvgArtifactSpec };

export interface SessionTreeNode {
  id: string;
  parent_id: string | null;
  root_session_id: string;
  depth: number;
  agent_id: string;
  agent_type: string | null;
  channel: string;
  status: "active" | "paused" | "completed" | "failed";
  title: string | null;
  model: string | null;
  message_count: number;
  tool_call_count: number;
  created_at: string;
  updated_at: string;
}

export interface SessionTreeResponse {
  nodes: SessionTreeNode[];
  total: number;
}

export interface SessionChildrenResponse {
  children: SessionTreeNode[];
  total: number;
}
