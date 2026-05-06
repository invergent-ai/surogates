import { createContext, useContext } from "react";
import type { AgentChatAdapter } from "./types";

export interface AgentChatAdapterContextValue {
  adapter: AgentChatAdapter;
  sessionId: string | null;
  onFileSelect?: (path: string) => void;
}

const AgentChatAdapterContext =
  createContext<AgentChatAdapterContextValue | null>(null);

export const AgentChatAdapterProvider = AgentChatAdapterContext.Provider;

export function useAgentChatAdapterContext(): AgentChatAdapterContextValue {
  const value = useContext(AgentChatAdapterContext);
  if (!value) {
    throw new Error(
      "useAgentChatAdapterContext must be used inside AgentChatAdapterProvider",
    );
  }
  return value;
}
