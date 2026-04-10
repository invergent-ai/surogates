// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Custom chat thread — uses ai-elements Conversation + Message
// with a compact, Claude Code-inspired layout.
//
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { ChatMessage } from "./chat-message";
import { ChatComposer } from "./chat-composer";
import { MessageSquareIcon } from "lucide-react";
import type { ChatMessage as ChatMessageType } from "@/hooks/use-session-runtime";

interface ChatThreadProps {
  messages: ChatMessageType[];
  isRunning: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  disabled?: boolean;
}

export function ChatThread({
  messages,
  isRunning,
  onSend,
  onStop,
  disabled = false,
}: ChatThreadProps) {
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <Conversation className="relative flex-1 min-h-0">
        <ConversationContent className="mx-auto max-w-3xl">
          {messages.length === 0 && !disabled ? (
            <ConversationEmptyState
              icon={<MessageSquareIcon className="size-6" />}
              title="Hello there!"
              description="How can I help you today?"
            />
          ) : (
            messages.map((msg, i) => (
              <ChatMessage
                key={msg.id}
                message={msg}
                isLast={i === messages.length - 1}
              />
            ))
          )}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>

      <div className="mx-auto w-full max-w-3xl px-4 pb-4 pt-2">
        <ChatComposer
          onSend={onSend}
          onStop={onStop}
          isRunning={isRunning}
          disabled={disabled}
        />
      </div>
    </div>
  );
}
