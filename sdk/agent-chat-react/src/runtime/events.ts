import type { AgentChatEventType } from "../types";

export const WORKSPACE_MUTATING_TOOLS: ReadonlySet<string> = new Set([
  "terminal",
  "write_file",
  "patch",
  "execute_code",
  "browser_screenshot",
]);

export const AGENT_CHAT_LISTENED_EVENTS = [
  "user.message",
  "llm.request",
  "llm.response",
  "llm.thinking",
  "llm.delta",
  "tool.call",
  "tool.result",
  "session.start",
  "session.pause",
  "session.resume",
  "session.complete",
  "session.fail",
  "session.done",
  "harness.wake",
  "harness.crash",
  "context.compact",
  "skill.invoked",
  "policy.denied",
  "stream.timeout",
  "expert.delegation",
  "expert.result",
  "expert.failure",
  "expert.endorse",
  "expert.override",
  "code.run_started",
  "code.run_progress",
  "code.run_result",
  "user.feedback",
  "artifact.created",
  "artifact.updated",
  "browser.provisioned",
  "browser.destroyed",
  "browser.control_granted",
  "browser.control_returned",
  "ask_user_question.response",
  "iteration.summary",
  "turn.summary",
] as const satisfies readonly AgentChatEventType[];

/** Membership set for the listened events above.  The reconciliation poll
 * receives every persisted event type (including ones the reducer has no
 * case for); applying an unhandled type would drive the reducer to
 * ``undefined``.  Filtering through this set keeps the poll path aligned
 * with the SSE path, which only ever subscribes to the listed types. */
export const AGENT_CHAT_LISTENED_EVENT_SET: ReadonlySet<string> = new Set(
  AGENT_CHAT_LISTENED_EVENTS,
);
