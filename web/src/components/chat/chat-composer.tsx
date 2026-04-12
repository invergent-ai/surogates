// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useMemo, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type { PromptInputMessage } from "@/components/ai-elements/prompt-input";
import type { TokenUsage } from "@/hooks/use-session-runtime";
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
} from "@/components/ai-elements/context";
import { Button } from "@/components/ui/button";
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
} from "@/components/ai-elements/prompt-input";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import {
  Command,
  CommandList,
  CommandGroup,
  CommandItem,
  CommandEmpty,
} from "@/components/ui/command";
import { listSkills, type SkillSummary } from "@/api/skills";

// ── Slash command entry ──────────────────────────────────────────────

interface SlashCommand {
  value: string;
  label: string;
  description: string;
}

/**
 * Convert a skill into a slash command entry.
 * Uses the trigger field if present, otherwise falls back to the skill name.
 */
function skillToCommand(skill: SkillSummary): SlashCommand {
  const trigger = skill.trigger
    ? skill.trigger.startsWith("/") ? skill.trigger : `/${skill.trigger}`
    : `/${skill.name}`;
  return {
    value: trigger,
    label: trigger,
    description: skill.description,
  };
}

// ── Props ────────────────────────────────────────────────────────────

interface ChatComposerProps {
  onSend: (text: string) => void;
  onStop: () => void;
  isRunning: boolean;
  disabled?: boolean;
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

// ── Inner component (has access to controller) ──────────────────────

function ChatComposerInner({
  onSend,
  onStop,
  isRunning,
  disabled = false,
  tokenUsage,
}: ChatComposerProps) {
  const { textInput } = usePromptInputController();
  const status = isRunning ? "streaming" : disabled ? "error" : "ready";

  // ── Load skills from backend ─────────────────────────────────────

  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [buttonMenuOpen, setButtonMenuOpen] = useState(false);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const showSlashMenu = !menuDismissed && (textInput.value.startsWith("/") || buttonMenuOpen);

  // Re-open when user types a new `/` after dismissal.
  if (menuDismissed && !textInput.value.startsWith("/") && !buttonMenuOpen) {
    setMenuDismissed(false);
  }

  // Re-fetch skills each time the slash menu opens.
  const [wasClosed, setWasClosed] = useState(true);
  if (showSlashMenu && wasClosed) {
    setWasClosed(false);
    listSkills()
      .then((res) => setSkills(res.skills))
      .catch(() => { /* best-effort */ });
  }
  if (!showSlashMenu && !wasClosed) {
    setWasClosed(true);
  }

  const slashCommands = useMemo(() => {
    const builtin: SlashCommand[] = [
      { value: "/clear", label: "/clear", description: "Clear conversation" },
      { value: "/compress", label: "/compress", description: "Compress context" },
    ];
    const fromSkills = skills.map(skillToCommand);
    return [...fromSkills, ...builtin];
  }, [skills]);

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
  const [prevQuery, setPrevQuery] = useState(searchQuery);

  // Reset selection when filter changes.
  if (searchQuery !== prevQuery) {
    setPrevQuery(searchQuery);
    setSelectedIndex(0);
  }

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
        handleCommandSelect(filteredCommands[selectedIndex].value);
      } else if (e.key === "Escape") {
        e.preventDefault();
        textInput.setInput("");
        setButtonMenuOpen(false);
        setMenuDismissed(true);
      } else if (e.key === "Tab") {
        e.preventDefault();
        handleCommandSelect(filteredCommands[selectedIndex].value);
      }
    },
    [menuOpen, filteredCommands, selectedIndex, handleCommandSelect, textInput],
  );

  // ── Submit ───────────────────────────────────────────────────────

  const handleSubmit = useCallback(
    (message: PromptInputMessage) => {
      const text = message.text.trim();
      if (!text || isRunning || disabled) return;
      onSend(text);
    },
    [onSend, isRunning, disabled],
  );

  // ── Render ───────────────────────────────────────────────────────

  return (
    <Popover open={menuOpen} onOpenChange={(open) => {
      if (!open) {
        setButtonMenuOpen(false);
      }
    }}>
      <PopoverAnchor asChild>
        <PromptInput onSubmit={handleSubmit}>
          <PromptInputBody>
            <PromptInputTextarea
              placeholder={disabled ? "Session disabled" : "Send a message..."}
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
                <span className="text-xs font-mono font-bold">/</span>
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
                  <span className="font-mono">{cmd.label}</span>
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
