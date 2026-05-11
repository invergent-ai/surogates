// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useMemo, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type { PromptInputMessage } from "../ai-elements/prompt-input";
import { useProviderAttachments } from "../ai-elements/prompt-input";
import type { AgentChatImageAttachment, AgentChatSlashCommand, TokenUsage } from "../../types";
import { useAgentChatAdapterContext } from "../../adapter-context";
import {
  Context,
  ContextCacheUsage,
  ContextContent,
  ContextContentBody,
  ContextContentHeader,
  ContextInputUsage,
  ContextOutputUsage,
  ContextReasoningUsage,
  ContextTrigger,
} from "../ai-elements/context";
import { Button } from "../ui/button";
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
  PromptInputProvider,
  usePromptInputController,
} from "../ai-elements/prompt-input";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "../ui/popover";
import {
  Command,
  CommandList,
  CommandGroup,
  CommandItem,
  CommandEmpty,
} from "../ui/command";

// ── Slash command entry ──────────────────────────────────────────────

type SlashCommand = AgentChatSlashCommand;

// ── Props ────────────────────────────────────────────────────────────

interface ChatComposerProps {
  onSend: (
    text: string,
    images?: AgentChatImageAttachment[],
  ) => void | Promise<void>;
  onStop: () => void | Promise<void>;
  isRunning: boolean;
  disabled?: boolean;
  disabledReason?: string;
  tokenUsage?: TokenUsage;
}

// ── Outer wrapper (provides controlled text state) ───────────────────

export function ChatComposer(props: ChatComposerProps) {
  return (
    <PromptInputProvider>
      <ChatComposerInner {...props} />
    </PromptInputProvider>
  );
}

// ── Attachment preview strip ─────────────────────────────────────────

function AttachmentPreviewStrip() {
  const attachments = useProviderAttachments();
  if (attachments.files.length === 0) return null;

  return (
    <div className="flex gap-2 px-3 pt-2 pb-1 flex-wrap">
      {attachments.files.map((file) => (
        <div key={file.id} className="relative group">
          {file.mediaType?.startsWith("image/") && file.url ? (
            <img
              src={file.url}
              alt={file.filename}
              className="h-16 w-16 rounded-lg border border-border object-cover"
            />
          ) : (
            <div className="h-16 w-16 rounded-lg border border-border bg-muted flex items-center justify-center text-[10px] text-muted-foreground truncate px-1">
              {file.filename}
            </div>
          )}
          <button
            type="button"
            onClick={() => attachments.remove(file.id)}
            className="absolute -top-1.5 -right-1.5 hidden group-hover:flex items-center justify-center w-4 h-4 rounded-full bg-destructive text-destructive-foreground text-[10px]"
          >
            &times;
          </button>
        </div>
      ))}
    </div>
  );
}

// ── Inner component (has access to controller) ──────────────────────

function ChatComposerInner({
  onSend,
  onStop,
  isRunning,
  disabled = false,
  disabledReason,
  tokenUsage,
}: ChatComposerProps) {
  const { adapter } = useAgentChatAdapterContext();
  const { textInput } = usePromptInputController();
  const status = isRunning ? "streaming" : disabled ? "error" : "ready";

  // ── Load skills from backend ─────────────────────────────────────

  const [adapterCommands, setAdapterCommands] = useState<SlashCommand[]>([]);
  const [buttonMenuOpen, setButtonMenuOpen] = useState(false);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const showSlashMenu = !menuDismissed && (textInput.value.startsWith("/") || buttonMenuOpen);

  // Re-open when user types a new `/` after dismissal.
  useEffect(() => {
    if (menuDismissed && !textInput.value.startsWith("/") && !buttonMenuOpen) {
      setMenuDismissed(false);
    }
  }, [buttonMenuOpen, menuDismissed, textInput.value]);

  // Re-fetch app-provided commands each time the slash menu opens.
  const [wasClosed, setWasClosed] = useState(true);
  useEffect(() => {
    if (showSlashMenu && wasClosed) {
      setWasClosed(false);
      adapter.listSlashCommands?.()
        .then(setAdapterCommands)
        .catch(() => { /* best-effort */ });
    }
    if (!showSlashMenu && !wasClosed) {
      setWasClosed(true);
    }
  }, [adapter, showSlashMenu, wasClosed]);

  const slashCommands = useMemo(() => {
    const builtin: SlashCommand[] = [
      { value: "/clear", label: "/clear", description: "Clear conversation" },
      { value: "/compress", label: "/compress", description: "Compress context" },
      { value: "/loop", label: "/loop", description: "Schedule recurring prompt" },
      { value: "/loop list", label: "/loop list", description: "List active loops" },
      { value: "/loop cancel", label: "/loop cancel", description: "Cancel a loop by ID" },
    ];
    return [...adapterCommands, ...builtin];
  }, [adapterCommands]);

  // ── Slash menu state ─────────────────────────────────────────────

  const searchQuery = showSlashMenu ? textInput.value.slice(1).toLowerCase() : "";

  const filteredCommands = useMemo(
    () =>
      slashCommands.filter(
        (cmd) =>
          cmd.value.slice(1).toLowerCase().includes(searchQuery) ||
          cmd.description.toLowerCase().includes(searchQuery),
      ),
    [slashCommands, searchQuery],
  );

  const [selectedIndex, setSelectedIndex] = useState(0);

  // Reset selection when filter changes.
  useEffect(() => {
    setSelectedIndex(0);
  }, [searchQuery]);

  useEffect(() => {
    setSelectedIndex((index) =>
      Math.min(index, Math.max(filteredCommands.length - 1, 0)),
    );
  }, [filteredCommands.length]);

  const menuOpen = showSlashMenu && filteredCommands.length > 0;

  const handleCommandSelect = useCallback(
    (commandValue: string) => {
      textInput.setInput(commandValue + " ");
      setButtonMenuOpen(false);
      setMenuDismissed(true);
    },
    [textInput],
  );

  // ── Key handling (runs before PromptInputTextarea internals) ─────

  const handleKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
      if (!menuOpen) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, filteredCommands.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const command = filteredCommands[selectedIndex];
        if (command) handleCommandSelect(command.value);
      } else if (e.key === "Escape") {
        e.preventDefault();
        textInput.setInput("");
        setButtonMenuOpen(false);
        setMenuDismissed(true);
      } else if (e.key === "Tab") {
        e.preventDefault();
        const command = filteredCommands[selectedIndex];
        if (command) handleCommandSelect(command.value);
      }
    },
    [menuOpen, filteredCommands, selectedIndex, handleCommandSelect, textInput],
  );

  // ── Submit ───────────────────────────────────────────────────────

  const handleSubmit = useCallback(
    async (message: PromptInputMessage) => {
      const text = message.text.trim();
      if (!text || disabled) return;

      // Convert file attachments to image blocks.
      const images: AgentChatImageAttachment[] = [];
      for (const file of message.files) {
        if (file.mediaType?.startsWith("image/") && file.url) {
          images.push({ data: file.url, mimeType: file.mediaType });
        }
      }
      if (isRunning) {
        await onStop();
      }
      await onSend(text, images.length > 0 ? images : undefined);
    },
    [onSend, onStop, isRunning, disabled],
  );

  // ── Render ───────────────────────────────────────────────────────

  return (
    <Popover open={menuOpen} onOpenChange={(open) => {
      if (!open) {
        setButtonMenuOpen(false);
      }
    }}>
      <AttachmentPreviewStrip />
      <PopoverAnchor asChild>
        <PromptInput onSubmit={handleSubmit} accept="image/*" multiple>
          <PromptInputBody>
            <PromptInputTextarea
              placeholder={
                disabled
                  ? disabledReason ?? "Session disabled"
                  : "Send a message..."
              }
              disabled={disabled}
              onKeyDown={handleKeyDown}
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
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                className="text-muted-foreground"
                onClick={(e) => {
                  e.preventDefault();
                  setButtonMenuOpen((v) => !v);
                }}
                aria-label="Slash commands"
              >
                <span className="text-xs  font-bold">/</span>
              </Button>
              {tokenUsage && tokenUsage.contextWindow > 0 && (
                <Context
                  usedTokens={tokenUsage.totalTokens}
                  maxTokens={tokenUsage.contextWindow}
                  modelId={tokenUsage.model}
                  usage={{
                    inputTokens: tokenUsage.inputTokens,
                    outputTokens: tokenUsage.outputTokens,
                    reasoningTokens: tokenUsage.reasoningTokens,
                    cachedInputTokens: tokenUsage.cachedInputTokens,
                    totalTokens: tokenUsage.totalTokens,
                    inputTokenDetails: undefined as never,
                    outputTokenDetails: undefined as never,
                  }}
                >
                  <ContextTrigger />
                  <ContextContent>
                    <ContextContentHeader />
                    <ContextContentBody>
                      {tokenUsage.totalTokens > 0 ? (
                        <>
                          <ContextInputUsage>
                            <div className="flex items-center justify-between text-xs">
                              <span className="text-muted-foreground">Input</span>
                              <span>{tokenUsage.inputTokens.toLocaleString()}</span>
                            </div>
                          </ContextInputUsage>
                          <ContextOutputUsage>
                            <div className="flex items-center justify-between text-xs">
                              <span className="text-muted-foreground">Output</span>
                              <span>{tokenUsage.outputTokens.toLocaleString()}</span>
                            </div>
                          </ContextOutputUsage>
                          {tokenUsage.reasoningTokens > 0 && (
                            <ContextReasoningUsage>
                              <div className="flex items-center justify-between text-xs">
                                <span className="text-muted-foreground">Reasoning</span>
                                <span>{tokenUsage.reasoningTokens.toLocaleString()}</span>
                              </div>
                            </ContextReasoningUsage>
                          )}
                          {tokenUsage.cachedInputTokens > 0 && (
                            <ContextCacheUsage>
                              <div className="flex items-center justify-between text-xs">
                                <span className="text-muted-foreground">Cache</span>
                                <span>{tokenUsage.cachedInputTokens.toLocaleString()}</span>
                              </div>
                            </ContextCacheUsage>
                          )}
                        </>
                      ) : (
                        <p className="text-xs text-muted-foreground text-center py-1">Empty</p>
                      )}
                    </ContextContentBody>
                    {tokenUsage.totalTokens > 0 && (
                      <div className="flex w-full items-center justify-end gap-3 bg-secondary p-3">
                        <Button
                          type="button"
                          size="xs"
                          onClick={() => onSend("/compress")}
                        >
                          Compress
                        </Button>
                      </div>
                    )}
                  </ContextContent>
                </Context>
              )}
            </PromptInputTools>
            <PromptInputSubmit status={status} onStop={onStop} />
          </PromptInputFooter>
        </PromptInput>
      </PopoverAnchor>
      <PopoverContent
        side="top"
        align="start"
        className="p-0"
        style={{ width: "var(--radix-popover-trigger-width)" }}
        onOpenAutoFocus={(e) => e.preventDefault()}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <Command value={filteredCommands[selectedIndex]?.value} filter={() => 1}>
          <CommandList>
            <CommandGroup>
              {filteredCommands.map((cmd) => (
                <CommandItem
                  key={cmd.value}
                  value={cmd.value}
                  onSelect={() => handleCommandSelect(cmd.value)}
                  className="grid grid-cols-[6rem_1fr] gap-2 [&_svg]:hidden"
                >
                  <span className="">{cmd.label}</span>
                  <span className="text-muted-foreground">
                    {cmd.description}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandEmpty>No commands found</CommandEmpty>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
