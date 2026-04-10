// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Custom chat thread component — replaces assistant-ui Thread.
// Renders messages directly from useSessionRuntime with a compact,
// Claude Code-inspired layout using Streamdown for markdown.
//
import { useEffect, useRef } from "react";
import { ChatMessage } from "./chat-message";
import { ChatComposer } from "./chat-composer";
import { ScrollArea } from "@/components/ui/scroll-area";
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
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages or streaming updates.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <ScrollArea className="flex-1 min-h-0">
        <div className="mx-auto w-full max-w-3xl px-4 pt-4 pb-4">
          {messages.length === 0 && !disabled && (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <h1 className="text-2xl font-semibold text-foreground">
                Hello there!
              </h1>
              <p className="mt-1 text-muted-foreground">
                How can I help you today?
              </p>
            </div>
          )}

          {messages.map((msg, i) => (
            <ChatMessage
              key={msg.id}
              message={msg}
              isLast={i === messages.length - 1}
            />
          ))}

          <div ref={bottomRef} />
        </div>
      </ScrollArea>

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
