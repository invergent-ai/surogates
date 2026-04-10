// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback } from "react";
import type { PromptInputMessage } from "@/components/ai-elements/prompt-input";
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
  PromptInputActionMenu,
  PromptInputActionMenuTrigger,
  PromptInputActionMenuContent,
  PromptInputActionAddAttachments,
} from "@/components/ai-elements/prompt-input";

interface ChatComposerProps {
  onSend: (text: string) => void;
  onStop: () => void;
  isRunning: boolean;
  disabled?: boolean;
}

export function ChatComposer({
  onSend,
  onStop,
  isRunning,
  disabled = false,
}: ChatComposerProps) {
  const status = isRunning ? "streaming" : disabled ? "error" : "ready";

  const handleSubmit = useCallback(
    (message: PromptInputMessage) => {
      const text = message.text.trim();
      if (!text || isRunning || disabled) return;
      onSend(text);
    },
    [onSend, isRunning, disabled],
  );

  return (
    <PromptInput onSubmit={handleSubmit}>
      <PromptInputBody>
        <PromptInputTextarea
          placeholder={disabled ? "Session disabled" : "Send a message..."}
          disabled={disabled}
        />
      </PromptInputBody>
      <PromptInputFooter>
        <PromptInputTools>
          <PromptInputActionMenu>
            <PromptInputActionMenuTrigger />
            <PromptInputActionMenuContent>
              <PromptInputActionAddAttachments />
            </PromptInputActionMenuContent>
          </PromptInputActionMenu>
        </PromptInputTools>
        <PromptInputSubmit status={status} onStop={onStop} />
      </PromptInputFooter>
    </PromptInput>
  );
}
