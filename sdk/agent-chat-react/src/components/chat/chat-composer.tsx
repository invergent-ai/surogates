// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  File as FileIcon,
  FileArchive,
  FileAudio,
  FileCode,
  FileSpreadsheet,
  FileText,
  FileVideo,
} from "lucide-react";
import type { PromptInputMessage } from "../ai-elements/prompt-input";
import { useProviderAttachments } from "../ai-elements/prompt-input";
import type {
  AgentChatImageAttachment,
  AgentChatPendingAttachment,
  AgentChatSlashCommand,
  TokenUsage,
} from "../../types";
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
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemMedia,
  ItemTitle,
} from "../ui/item";
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
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "../ui/command";

// ── Slash command entry ──────────────────────────────────────────────

type SlashCommand = AgentChatSlashCommand;

// ── Props ────────────────────────────────────────────────────────────

export interface ChatComposerError {
  /**
   * Stable error code so callers can route different rejections (e.g.
   * show a different toast variant per code) without parsing the
   * human-readable message.
   */
  code:
    | "accept"
    | "max_files"
    | "max_file_size"
    | "max_images"
    | "max_image_size"
    | "max_attachments"
    | "max_attachment_size"
    | "max_attachments_total";
  /** Display-ready, single-sentence reason. */
  message: string;
}

interface ChatComposerProps {
  onSend: (
    text: string,
    images?: AgentChatImageAttachment[],
    attachments?: AgentChatPendingAttachment[],
  ) => void | Promise<void>;
  onStop: () => void | Promise<void>;
  isRunning: boolean;
  disabled?: boolean;
  disabledReason?: string;
  tokenUsage?: TokenUsage;
  /**
   * Optional handler for client-side rejections (size/count caps,
   * accept-pattern misses).  Without it, rejections are silent — pass
   * a toast wiring at the host-app layer if you want them surfaced.
   */
  onComposerError?: (err: ChatComposerError) => void;
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

function iconForMime(mime?: string) {
  if (!mime) return FileIcon;
  if (mime.startsWith("audio/")) return FileAudio;
  if (mime.startsWith("video/")) return FileVideo;
  if (mime === "application/pdf" || mime.startsWith("text/")) return FileText;
  if (
    mime === "application/json" ||
    mime === "application/xml" ||
    mime.endsWith("+xml") ||
    mime.endsWith("+json") ||
    mime.includes("javascript") ||
    mime.includes("typescript")
  ) {
    return FileCode;
  }
  if (
    mime === "application/zip" ||
    mime === "application/x-7z-compressed" ||
    mime === "application/x-tar" ||
    mime === "application/gzip" ||
    mime === "application/x-rar-compressed"
  ) {
    return FileArchive;
  }
  if (
    mime === "text/csv" ||
    mime === "application/vnd.ms-excel" ||
    mime.includes("spreadsheet")
  ) {
    return FileSpreadsheet;
  }
  return FileIcon;
}

function formatBytes(n?: number): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "";
  for (const [unit, divisor] of [
    ["GB", 1_000_000_000],
    ["MB", 1_000_000],
    ["KB", 1_000],
  ] as const) {
    if (n >= divisor) return `${(n / divisor).toFixed(1)} ${unit}`;
  }
  return `${n} B`;
}

function AttachmentPreviewStrip() {
  const attachments = useProviderAttachments();
  if (attachments.files.length === 0) return null;

  return (
    <div className="flex gap-2 px-3 pt-2 pb-1 flex-wrap">
      {attachments.files.map((file) => {
        const isImage =
          file.mediaType?.startsWith("image/") && !!file.url;
        const sizeLabel = formatBytes(file.file?.size);
        const Icon = iconForMime(file.mediaType);

        // Single uniform Item layout for both images and non-images:
        // the only thing that differs is the ItemMedia slot (a real
        // thumbnail when we have one, a mime-bucket icon otherwise).
        // Class overrides on the outer Item drop the list-row defaults
        // (``w-full``, ``rounded-none``) so the chips sit inline at
        // intrinsic width.
        return (
          <Item
            key={file.id}
            variant="outline"
            size="xs"
            className="group w-auto max-w-[18rem] rounded-md"
            title={file.filename}
          >
            <ItemMedia variant={isImage ? "image" : "icon"}>
              {isImage ? (
                <img src={file.url} alt={file.filename} />
              ) : (
                <Icon />
              )}
            </ItemMedia>
            <ItemContent className="min-w-0">
              {/*
                Override ``flex`` (which the shadcn ItemTitle bakes
                into its base classes) with ``block`` so the
                ``truncate`` utility's ``text-overflow: ellipsis``
                actually takes effect.  Flex would override the
                ``display: -webkit-box`` that line-clamp-1 relies on
                AND defeat text-overflow.  ``min-w-0`` on the parent
                ItemContent is also required: without it, flex's
                default ``min-width: auto`` lets the title push past
                the Item's max-width on filenames with long
                unbreakable runs.
              */}
              <ItemTitle className="block w-full truncate normal-case font-medium text-foreground">
                {file.filename}
              </ItemTitle>
              {sizeLabel && (
                <ItemDescription className="text-xs">
                  {sizeLabel}
                </ItemDescription>
              )}
            </ItemContent>
            <ItemActions>
              <button
                type="button"
                onClick={() => attachments.remove(file.id)}
                aria-label={`Remove ${file.filename}`}
                className="hidden group-hover:flex items-center justify-center w-4 h-4 rounded-full bg-destructive text-destructive-foreground text-[10px]"
              >
                &times;
              </button>
            </ItemActions>
          </Item>
        );
      })}
    </div>
  );
}

// ── Inner component (has access to controller) ──────────────────────

// Per-message caps mirror the server-side limits so the composer can
// reject without a server round-trip.  Keep these in sync with
// _MAX_IMAGES_PER_MESSAGE / _MAX_IMAGE_BYTES /
// _MAX_ATTACHMENTS_PER_MESSAGE / _MAX_ATTACHMENT_BYTES /
// _MAX_ATTACHMENTS_TOTAL_BYTES on the harness side.
const MAX_IMAGES_PER_MESSAGE = 5;
const MAX_IMAGE_BYTES = 20_000_000;
const MAX_ATTACHMENTS_PER_MESSAGE = 10;
const MAX_ATTACHMENT_BYTES = 50_000_000;
const MAX_ATTACHMENTS_TOTAL_BYTES = 200_000_000;

function ChatComposerInner({
  onSend,
  onStop,
  isRunning,
  disabled = false,
  disabledReason,
  tokenUsage,
  onComposerError,
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

  const builtinCommands = useMemo<SlashCommand[]>(
    () => [
      { value: "/clear", label: "/clear", description: "Clear conversation" },
      { value: "/compress", label: "/compress", description: "Compress context" },
      { value: "/goal", label: "/goal", description: "Define an outcome goal" },
      { value: "/goal status", label: "/goal status", description: "Show outcome goal status" },
      { value: "/goal pause", label: "/goal pause", description: "Pause automatic goal continuation" },
      { value: "/goal resume", label: "/goal resume", description: "Resume a paused goal" },
      { value: "/goal clear", label: "/goal clear", description: "Clear the current goal" },
      { value: "/loop", label: "/loop", description: "Schedule recurring prompt" },
      { value: "/loop list", label: "/loop list", description: "List active loops" },
      { value: "/loop cancel", label: "/loop cancel", description: "Cancel a loop by ID" },
    ],
    [],
  );

  // ── Slash menu state ─────────────────────────────────────────────

  // The CommandInput inside the popup is the canonical search input
  // while the menu is open.  Its value mirrors whatever follows the
  // leading ``/`` in the textarea — typing in either keeps both in
  // sync (onValueChange writes back to the textarea, the textarea's
  // controller updates ``searchQuery`` on every render).  cmdk does
  // the filtering + arrow-key navigation + scroll-into-view itself,
  // so no manual ``selectedIndex`` or layout effect is needed: the
  // command palette behaves exactly like the shadcn example.
  const searchQuery = showSlashMenu ? textInput.value.slice(1) : "";

  const menuOpen = showSlashMenu;

  // Selecting a command closes the popup; cmdk + Radix Popover then
  // tear down the CommandInput, which is the currently-focused
  // element.  Without an explicit hand-off, focus falls onto the body
  // and the user has to click back into the chat input before they
  // can keep typing.  We stash a ref on the textarea below and call
  // .focus() on it after the state updates flush, then move the
  // caret to the end so the user can continue typing arguments
  // straight after the inserted command.
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const handleCommandSelect = useCallback(
    (commandValue: string) => {
      textInput.setInput(commandValue + " ");
      setButtonMenuOpen(false);
      setMenuDismissed(true);
      // requestAnimationFrame waits for Radix to finish its
      // close-animation focus-restoration step; if we focus too
      // early, Radix's own onCloseAutoFocus pulls focus off the
      // textarea again.
      requestAnimationFrame(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;
        textarea.focus();
        const end = textarea.value.length;
        textarea.setSelectionRange(end, end);
      });
    },
    [textInput],
  );

  const handleSearchChange = useCallback(
    (next: string) => {
      // Mirror the popup's CommandInput back into the textarea so the
      // two stay in sync and the chat submit picks up exactly what
      // the user sees in the palette.
      textInput.setInput("/" + next);
    },
    [textInput],
  );

  // ── Submit ───────────────────────────────────────────────────────

  const handleSubmit = useCallback(
    async (message: PromptInputMessage) => {
      const text = message.text.trim();
      if (!text || disabled) return;

      // Split incoming files by MIME bucket: images go through the
      // existing vision-inline path (base64 data URL on the message
      // body), everything else becomes a pending attachment uploaded
      // to the workspace by the runtime.
      const images: AgentChatImageAttachment[] = [];
      const pending: AgentChatPendingAttachment[] = [];

      for (const file of message.files) {
        if (file.mediaType?.startsWith("image/") && file.url) {
          images.push({ data: file.url, mimeType: file.mediaType });
          continue;
        }
        if (!file.file) {
          // Defensive: PromptInputProvider/local both stamp file: on
          // each item. If a future consumer constructs a FileUIPart
          // without it, drop the entry rather than pretending we can
          // upload nothing.
          continue;
        }
        pending.push({
          file: file.file,
          filename: file.filename ?? file.file.name,
          mimeType: file.mediaType ?? file.file.type ?? undefined,
          size: file.file.size,
        });
      }

      // Split-aware caps — the <PromptInput maxFiles/maxFileSize> caps
      // are a coarse first line that ignores the image/non-image
      // distinction; we re-check per bucket here.
      if (images.length > MAX_IMAGES_PER_MESSAGE) {
        onComposerError?.({
          code: "max_images",
          message: `Maximum ${MAX_IMAGES_PER_MESSAGE} images per message.`,
        });
        return;
      }
      if (pending.length > MAX_ATTACHMENTS_PER_MESSAGE) {
        onComposerError?.({
          code: "max_attachments",
          message: `Maximum ${MAX_ATTACHMENTS_PER_MESSAGE} attachments per message.`,
        });
        return;
      }
      for (const img of images) {
        // Best-effort image-size guard: a base64-encoded image is
        // roughly 4/3 the raw size, so 27 MB of base64 ≈ 20 MB raw.
        if (img.data.length > Math.ceil(MAX_IMAGE_BYTES * 4 / 3)) {
          onComposerError?.({
            code: "max_image_size",
            message: `Image exceeds ${MAX_IMAGE_BYTES / 1_000_000} MB.`,
          });
          return;
        }
      }
      let totalAttachmentBytes = 0;
      for (const a of pending) {
        if (a.file.size > MAX_ATTACHMENT_BYTES) {
          onComposerError?.({
            code: "max_attachment_size",
            message: `"${a.filename}" exceeds ${MAX_ATTACHMENT_BYTES / 1_000_000} MB.`,
          });
          return;
        }
        totalAttachmentBytes += a.file.size;
      }
      if (totalAttachmentBytes > MAX_ATTACHMENTS_TOTAL_BYTES) {
        onComposerError?.({
          code: "max_attachments_total",
          message: `Attachments exceed ${MAX_ATTACHMENTS_TOTAL_BYTES / 1_000_000} MB total for this message.`,
        });
        return;
      }

      if (isRunning) {
        await onStop();
      }
      await onSend(
        text,
        images.length > 0 ? images : undefined,
        pending.length > 0 ? pending : undefined,
      );
    },
    [onSend, onStop, onComposerError, isRunning, disabled],
  );

  const handlePromptInputError = useCallback(
    (err: { code: "max_files" | "max_file_size" | "accept"; message: string }) => {
      onComposerError?.({ code: err.code, message: err.message });
    },
    [onComposerError],
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
        <PromptInput
          onSubmit={handleSubmit}
          multiple
          maxFiles={MAX_IMAGES_PER_MESSAGE + MAX_ATTACHMENTS_PER_MESSAGE}
          maxFileSize={MAX_ATTACHMENT_BYTES}
          onError={handlePromptInputError}
        >
          <PromptInputBody>
            <PromptInputTextarea
              ref={textareaRef}
              placeholder={
                disabled
                  ? disabledReason ?? "Session disabled"
                  : "Send a message..."
              }
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
        className="overflow-hidden p-0"
        style={{ width: "var(--radix-popover-trigger-width)" }}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        {/*
          Native shadcn / cmdk command palette: the CommandInput owns
          focus while the popup is open, cmdk runs the filter
          (matching ``value`` plus the ``keywords`` we pass per item,
          so descriptions count toward matches), and cmdk handles
          arrow keys / Enter / scroll-into-view itself.  The CommandInput
          mirrors the textarea so the chat input still reflects the
          query and sending the message still works the same way.
        */}
        <Command>
          <CommandInput
            placeholder="Type a command or search..."
            value={searchQuery}
            onValueChange={handleSearchChange}
          />
          <CommandList>
            <CommandEmpty>No commands found.</CommandEmpty>
            {adapterCommands.length > 0 && (
              <CommandGroup heading="Skills">
                {adapterCommands.map((cmd) => (
                  <CommandItem
                    key={cmd.value}
                    value={cmd.value}
                    keywords={[cmd.description]}
                    onSelect={() => handleCommandSelect(cmd.value)}
                    className="grid grid-cols-[12rem_1fr] items-baseline gap-3 [&_svg]:hidden"
                  >
                    <span
                      className="font-mono text-foreground truncate"
                      title={cmd.label}
                    >
                      {cmd.label}
                    </span>
                    <span
                      className="min-w-0 truncate text-xs text-muted-foreground"
                      title={cmd.description}
                    >
                      {cmd.description}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {adapterCommands.length > 0 && builtinCommands.length > 0 && (
              <CommandSeparator />
            )}
            {builtinCommands.length > 0 && (
              <CommandGroup heading="Built-in commands">
                {builtinCommands.map((cmd) => (
                  <CommandItem
                    key={cmd.value}
                    value={cmd.value}
                    keywords={[cmd.description]}
                    onSelect={() => handleCommandSelect(cmd.value)}
                    className="grid grid-cols-[12rem_1fr] items-baseline gap-3 [&_svg]:hidden"
                  >
                    <span
                      className="font-mono text-foreground truncate"
                      title={cmd.label}
                    >
                      {cmd.label}
                    </span>
                    <span
                      className="min-w-0 truncate text-xs text-muted-foreground"
                      title={cmd.description}
                    >
                      {cmd.description}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
