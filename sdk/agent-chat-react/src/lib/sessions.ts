import type { AgentChatSession } from "../types";

export function isScheduledRunSession(
  session: AgentChatSession | null | undefined,
): boolean {
  return Boolean(
    session?.channel === "scheduled" || session?.config?.scheduled_session_id,
  );
}
