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
  model?: string;
  system?: string;
  tools?: string[];
  sandbox?: { image?: string };
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
  | "harness.crash";
