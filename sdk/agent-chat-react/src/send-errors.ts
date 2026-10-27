import type { AgentChatErrorInfo } from "./types";

/**
 * What an adapter throws from `sendMessage` when the agent's paywall refuses
 * the turn. The runtime turns it into a buy-access card instead of a failed
 * send, so the buyer gets a clickable link rather than raw text.
 */
export class AgentChatPaywallError extends Error {
  readonly code: string;
  readonly buyUrl?: string;

  constructor(message: string, code: string, buyUrl?: string) {
    super(message);
    this.name = "AgentChatPaywallError";
    this.code = code;
    this.buyUrl = buyUrl;
  }
}

export function sendErrorInfo(error: unknown): AgentChatErrorInfo {
  if (error instanceof AgentChatPaywallError) {
    return {
      category: "paywall",
      title: error.message,
      detail: "",
      retryable: false,
      paywall: { code: error.code, buyUrl: error.buyUrl },
    };
  }
  const detail = error instanceof Error ? error.message : String(error ?? "");
  return {
    category: "network",
    title: "Your message was not sent",
    detail: detail || "send failed",
    retryable: false,
  };
}
