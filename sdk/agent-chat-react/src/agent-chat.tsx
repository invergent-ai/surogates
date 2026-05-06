import type { AgentChatAdapter } from "./types";

export interface AgentChatProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  onFileSelect?: (path: string) => void;
  disabled?: boolean;
}

export function AgentChat(_props: AgentChatProps) {
  return null;
}
