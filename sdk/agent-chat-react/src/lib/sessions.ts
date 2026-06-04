import type { AgentChatSession } from "../types";

export function isScheduledRunSession(
  session: AgentChatSession | null | undefined,
): boolean {
  return Boolean(
    session?.channel === "scheduled" || session?.config?.scheduled_session_id,
  );
}

// Channels assigned by the harness when it spawns a child session on
// behalf of the running agent (delegate_task, spawn_worker, background
// task spawner).  These are NOT user chat threads -- the parent's
// LLM is the only authoritative voice and is polling/awaiting the
// child's final result.  Letting the user type into one either bends
// the child's goal mid-run or races the parent's completion read.
const _SUB_AGENT_CHANNELS = new Set([
  "delegation",
  "worker",
  "task",
]);

export function isSubAgentSession(
  session: AgentChatSession | null | undefined,
): boolean {
  if (!session) return false;
  if (session.parentId) return true;
  if (session.channel && _SUB_AGENT_CHANNELS.has(session.channel)) return true;
  return false;
}

export interface ReadOnlyReason {
  readOnly: boolean;
  reason?: string;
}

/**
 * Combined read-only check for the chat composer.  Returns the
 * specific reason so the host-facing banner is precise rather than a
 * generic "session disabled".
 */
export function readOnlyReasonForSession(
  session: AgentChatSession | null | undefined,
): ReadOnlyReason {
  if (isScheduledRunSession(session)) {
    return { readOnly: true, reason: "Scheduled run is read-only" };
  }
  if (isSubAgentSession(session)) {
    return {
      readOnly: true,
      reason: "Sub-task started by parent session — read-only",
    };
  }
  return { readOnly: false };
}
