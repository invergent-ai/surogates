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
  ChevronRightIcon,
  ClockIcon,
  CloudIcon,
  FolderIcon,
  GlobeIcon,
  HardDriveIcon,
  PaperclipIcon,
  PlusIcon,
  SparklesIcon,
  TerminalIcon,
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
  PromptInputButton,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
  PromptInputProvider,
  usePromptInputController,
} from "../ai-elements/prompt-input";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "../ui/popover";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
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

  // ── Pane toggles ──────────────────────────────────────────────────
  // Optional. When provided, render a button in the composer tools that
  // toggles the corresponding pane. The button shows an active style
  // when the pane is visible.
  showBrowser?: boolean;
  onToggleBrowser?: () => void;
  showWorkspace?: boolean;
  onToggleWorkspace?: () => void;
  /** When false (default), the browser toggle button is omitted entirely. */
  canShowBrowser?: boolean;
  /** When false (default), the workspace toggle button is omitted entirely. */
  canShowWorkspace?: boolean;

  // ── Simple/Expert view-mode toggle ───────────────────────────────
  // Optional. When ``onViewModeChange`` is provided, a two-segment
  // Simple/Expert toggle is rendered in the tools row.
  viewMode?: "simple" | "expert";
  onViewModeChange?: (mode: "simple" | "expert") => void;
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
  showBrowser = false,
  onToggleBrowser,
  showWorkspace = false,
  onToggleWorkspace,
  canShowBrowser = false,
  canShowWorkspace = false,
  viewMode = "simple",
  onViewModeChange,
}: ChatComposerProps) {
  const { adapter } = useAgentChatAdapterContext();
  const { textInput, attachments } = usePromptInputController();
  const status = isRunning ? "streaming" : disabled ? "error" : "ready";

  // ── Load skills from backend ─────────────────────────────────────

  const [adapterCommands, setAdapterCommands] = useState<SlashCommand[]>([]);
  const [buttonMenuOpen, setButtonMenuOpen] = useState(false);
  const [menuMode, setMenuMode] = useState<
    "commands" | "skills" | "scheduled" | "all"
  >("all");
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const showSlashMenu = !menuDismissed && (textInput.value.startsWith("/") || buttonMenuOpen);

  // Re-open when user types a new `/` after dismissal.
  useEffect(() => {
    if (menuDismissed && !textInput.value.startsWith("/") && !buttonMenuOpen) {
      setMenuDismissed(false);
    }
  }, [buttonMenuOpen, menuDismissed, textInput.value]);

  // Slash typing shows the combined commands + skills menu.
  useEffect(() => {
    if (textInput.value.startsWith("/")) {
      setMenuMode("all");
    }
  }, [textInput.value]);

  // Re-fetch app-provided skills each time the menu opens with skills in scope.
  useEffect(() => {
    if (showSlashMenu && (menuMode === "skills" || menuMode === "all")) {
      adapter.listSlashCommands?.()
        .then(setAdapterCommands)
        .catch(() => { /* best-effort */ });
    }
  }, [adapter, showSlashMenu, menuMode]);

  const builtinCommands = useMemo<SlashCommand[]>(
    () => [
      { value: "/clear", label: "/clear", description: "Clear conversation" },
      { value: "/compress", label: "/compress", description: "Compress context" },
      { value: "/goal", label: "/goal", description: "Define an outcome goal" },
      { value: "/goal status", label: "/goal status", description: "Show outcome goal status" },
      { value: "/goal pause", label: "/goal pause", description: "Pause automatic goal continuation" },
      { value: "/goal resume", label: "/goal resume", description: "Resume a paused goal" },
      { value: "/goal clear", label: "/goal clear", description: "Clear the current goal" },
      { value: "/mission ", label: "/mission", description: "Start an orchestrated rubric-judged mission" },
      { value: "/mission status", label: "/mission status", description: "Show current mission status" },
      { value: "/mission pause", label: "/mission pause", description: "Pause the mission evaluator" },
      { value: "/mission resume", label: "/mission resume", description: "Resume a paused mission" },
      { value: "/mission cancel", label: "/mission cancel", description: "Cancel the mission" },
      { value: "/loop", label: "/loop", description: "Schedule recurring prompt" },
      { value: "/loop list", label: "/loop list", description: "List active loops" },
      { value: "/loop cancel", label: "/loop cancel", description: "Cancel a loop by ID" },
    ],
    [],
  );

  const scheduledExamples = useMemo<SlashCommand[]>(
    () => [
      { value: "/loop list", label: "/loop list", description: "List your active scheduled tasks" },
      { value: "/loop cancel ", label: "/loop cancel <id>", description: "Cancel a scheduled task by ID" },
      { value: "/loop 5m check the deployment status and surface any failures", label: "/loop 5m deployment check", description: "Every 5 minutes: poll deployment status" },
      { value: "/loop 10m pull the build queue and report any stuck jobs", label: "/loop 10m build queue", description: "Every 10 minutes: review the build queue" },
      { value: "/loop 15m triage new PRs assigned to me", label: "/loop 15m PR triage", description: "Every 15 minutes: triage incoming PRs" },
      { value: "/loop 30m summarize new threads in support inbox", label: "/loop 30m support inbox", description: "Every 30 minutes: summarize support traffic" },
      { value: "/loop 1h review the on-call dashboard and flag anomalies", label: "/loop 1h on-call check", description: "Hourly: review the on-call dashboard" },
      { value: "/loop 2h scan production error rates and alert if elevated", label: "/loop 2h error scan", description: "Every 2 hours: production error scan" },
      { value: "/loop 1d give me a morning briefing of yesterday's activity", label: "/loop 1d daily briefing", description: "Daily: morning briefing of prior day" },
      { value: "/loop every 5 minutes check whether CI on the current branch has finished", label: "/loop every 5 minutes (verbose)", description: "Verbose interval form" },
      { value: "/loop watch the active deploy and notify me when it stabilises or rolls back", label: "/loop <prompt> (dynamic)", description: "Dynamic loop — model self-paces 1m–1h via loop_wait" },
      { value: "/loop babysit the long-running data migration and only ping me on errors or completion", label: "/loop migration watcher (dynamic)", description: "Dynamic loop — best when cadence is unpredictable" },
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

  // When the slash popup closes — by command selection, Escape, or
  // click-outside — cmdk + Radix tear down the CommandInput that was
  // holding focus.  Without an explicit hand-off, focus falls onto
  // the body and the user has to click back into the chat input.
  // We stash a ref on the textarea below and route every "the user
  // is done with the popup" path through this helper.
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const focusTextareaAtEnd = useCallback(() => {
    // requestAnimationFrame waits for Radix's own close-focus step to
    // run first; focusing synchronously inside an onSelect or
    // onEscapeKeyDown handler races Radix and the textarea loses
    // focus on the next tick.
    requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus();
      const end = textarea.value.length;
      textarea.setSelectionRange(end, end);
    });
  }, []);

  const handleCommandSelect = useCallback(
    (commandValue: string) => {
      textInput.setInput(`${commandValue} `);
      setButtonMenuOpen(false);
      setMenuDismissed(true);
      focusTextareaAtEnd();
    },
    [textInput, focusTextareaAtEnd],
  );

  const handleSearchChange = useCallback(
    (next: string) => {
      // Mirror the popup's CommandInput back into the textarea so the
      // two stay in sync and the chat submit picks up exactly what
      // the user sees in the palette.
      textInput.setInput(`/${next}`);
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
    <Popover
      open={menuOpen}
      onOpenChange={(open) => {
        if (!open) {
          // Radix fires onOpenChange(false) for both Escape and
          // click-outside.  Without latching ``menuDismissed`` here
          // the next render would recompute ``showSlashMenu`` from
          // the textarea (which still starts with ``/``) and the
          // popup would immediately reopen, undoing the user's
          // dismissal.  ``menuDismissed`` resets in the useEffect
          // at the top of this component as soon as the textarea
          // stops starting with ``/`` (or the button menu fires),
          // so the next genuine ``/`` press reopens the popup.
          setButtonMenuOpen(false);
          setMenuDismissed(true);
        }
      }}
    >
      <AttachmentPreviewStrip />
      {viewMode === "expert" && (
        <div className="flex flex-wrap items-center justify-end gap-2 px-1 pb-2">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="rounded-sm -uppercase font-display bg-white dark:bg-accent border-2 border-accent"
            onClick={() => {
              setMenuMode("commands");
              setMenuDismissed(false);
              setButtonMenuOpen(true);
            }}
          >
            <TerminalIcon />
            Commands
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="rounded-sm -uppercase font-display bg-white dark:bg-accent border-2 border-accent"
            onClick={() => {
              setMenuMode("skills");
              setMenuDismissed(false);
              setButtonMenuOpen(true);
            }}
          >
            <SparklesIcon />
            Skills
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="rounded-sm -uppercase font-display bg-white dark:bg-accent border-2 border-accent"
            onClick={() => {
              setMenuMode("scheduled");
              setMenuDismissed(false);
              setButtonMenuOpen(true);
            }}
          >
            <ClockIcon />
            Scheduled Tasks
          </Button>
        </div>
      )}
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
              <Popover open={addMenuOpen} onOpenChange={setAddMenuOpen}>
                <PopoverTrigger asChild>
                  <PromptInputButton aria-label="Add">
                    <PlusIcon className="size-4" />
                  </PromptInputButton>
                </PopoverTrigger>
                <PopoverContent
                  side="top"
                  align="start"
                  className="w-64 overflow-hidden rounded-xl p-1"
                >
                  <Command>
                    <CommandList>
                      <CommandItem
                        disabled
                        className="gap-3 rounded-md px-3 py-2 [&>svg:last-child]:hidden"
                      >
                        <CloudIcon className="size-4 shrink-0 text-sky-500" />
                        Add from OneDrive
                        <ChevronRightIcon className="ml-auto size-4 shrink-0 text-muted-foreground" />
                      </CommandItem>
                      <CommandItem
                        disabled
                        className="gap-3 rounded-md px-3 py-2 [&>svg:last-child]:hidden"
                      >
                        <HardDriveIcon className="size-4 shrink-0 text-emerald-500" />
                        Add from Google Drive
                      </CommandItem>
                      <CommandItem
                        onSelect={() => {
                          setAddMenuOpen(false);
                          attachments.openFileDialog();
                        }}
                        className="gap-3 rounded-md px-3 py-2 [&>svg:last-child]:hidden"
                      >
                        <PaperclipIcon className="size-4 shrink-0 text-muted-foreground" />
                        Add local files
                      </CommandItem>
                    </CommandList>
                  </Command>
                </PopoverContent>
              </Popover>
              {canShowBrowser && onToggleBrowser && (
                <PromptInputButton
                  aria-label={
                    showBrowser ? "Hide browser pane" : "Show browser pane"
                  }
                  aria-pressed={showBrowser}
                  onClick={onToggleBrowser}
                  tooltip={
                    showBrowser ? "Hide browser pane" : "Show browser pane"
                  }
                  className={
                    showBrowser
                      ? "bg-accent text-foreground"
                      : undefined
                  }
                >
                  <GlobeIcon className="size-4" />
                </PromptInputButton>
              )}
              {canShowWorkspace && onToggleWorkspace && (
                <PromptInputButton
                  aria-label={
                    showWorkspace
                      ? "Hide workspace pane"
                      : "Show workspace pane"
                  }
                  aria-pressed={showWorkspace}
                  onClick={onToggleWorkspace}
                  tooltip={
                    showWorkspace
                      ? "Hide workspace pane"
                      : "Show workspace pane"
                  }
                  className={
                    showWorkspace
                      ? "bg-accent text-foreground"
                      : undefined
                  }
                >
                  <FolderIcon className="size-4" />
                </PromptInputButton>
              )}
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
                  <ContextTrigger
                    size="icon-sm"
                    className="rounded-sm border border-input/70 text-foreground hover:bg-accent"
                  />
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
                      <div className="flex w-full items-center justify-end gap-3 bg-secondary p-2">
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
            <div className="flex items-center gap-2">
              {onViewModeChange && (
                <div
                  role="group"
                  aria-label="Chat view mode"
                  className="inline-flex overflow-hidden rounded-md border border-border"
                >
                  <button
                    type="button"
                    aria-pressed={viewMode === "simple"}
                    onClick={() => onViewModeChange("simple")}
                    className={
                      viewMode === "simple"
                        ? "bg-accent px-2 py-1 text-xs text-foreground"
                        : "px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                    }
                  >
                    Simple
                  </button>
                  <button
                    type="button"
                    aria-pressed={viewMode === "expert"}
                    onClick={() => onViewModeChange("expert")}
                    className={
                      viewMode === "expert"
                        ? "bg-accent px-2 py-1 text-xs text-foreground"
                        : "px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                    }
                  >
                    Advanced
                  </button>
                </div>
              )}
              <PromptInputSubmit
                status={status}
                onStop={onStop}
                className="md:size-8"
              />
            </div>
          </PromptInputFooter>
        </PromptInput>
      </PopoverAnchor>
      <PopoverContent
        side="top"
        align="start"
        className="overflow-hidden rounded-xl p-0"
        style={{ width: "var(--radix-popover-trigger-width)" }}
        onCloseAutoFocus={(e) => e.preventDefault()}
        onEscapeKeyDown={() => {
          // Escape is the canonical "back out of the popup, keep
          // typing in the chat" gesture.  Click-outside is
          // deliberately NOT routed here — if the user clicked some
          // other widget on the page they expect focus to land
          // wherever they clicked, not snap back to the textarea.
          focusTextareaAtEnd();
        }}
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
          {menuMode === "skills" ? (
            <CommandInput placeholder="Search skills..." />
          ) : menuMode === "scheduled" ? (
            <CommandInput placeholder="Search scheduled tasks..." />
          ) : (
            <CommandInput
              placeholder="Type a command or search..."
              value={searchQuery}
              onValueChange={handleSearchChange}
            />
          )}
          <CommandList>
            <CommandEmpty>
              {menuMode === "skills"
                ? "No skills found."
                : menuMode === "scheduled"
                ? "No scheduled tasks found."
                : "No commands found."}
            </CommandEmpty>
            {menuMode === "scheduled" &&
              scheduledExamples.map((cmd) => (
                <CommandItem
                  key={cmd.value}
                  value={cmd.value}
                  keywords={[cmd.description]}
                  onSelect={() => handleCommandSelect(cmd.value)}
                  className="grid grid-cols-[14rem_1fr] items-baseline gap-3 [&_svg]:hidden"
                >
                  <span
                    className="font-mono font-semibold text-foreground truncate"
                    title={cmd.label}
                  >
                    {cmd.label}
                  </span>
                  <span
                    className="min-w-0 truncate text-sm text-muted-foreground"
                    title={cmd.description}
                  >
                    {cmd.description}
                  </span>
                </CommandItem>
              ))}
            {(menuMode === "commands" || menuMode === "all") &&
              builtinCommands.map((cmd) => (
                <CommandItem
                  key={cmd.value}
                  value={cmd.value}
                  keywords={[cmd.description]}
                  onSelect={() => handleCommandSelect(cmd.value)}
                  className="grid grid-cols-[12rem_1fr] items-baseline gap-3 [&_svg]:hidden"
                >
                  <span
                    className="font-mono font-semibold text-foreground truncate"
                    title={cmd.label}
                  >
                    {cmd.label}
                  </span>
                  <span
                    className="min-w-0 truncate text-sm text-muted-foreground"
                    title={cmd.description}
                  >
                    {cmd.description}
                  </span>
                </CommandItem>
              ))}
            {(menuMode === "skills" || menuMode === "all") &&
              adapterCommands.map((cmd) => (
                <CommandItem
                  key={cmd.value}
                  value={cmd.value}
                  // Including "expert" as a fuzzy-match keyword lets the
                  // user type "/expert" to list every specialist at once.
                  keywords={
                    cmd.isExpert
                      ? [cmd.description, "expert"]
                      : [cmd.description]
                  }
                  onSelect={() => handleCommandSelect(cmd.value)}
                  className="grid grid-cols-[12rem_1fr] items-baseline gap-3 [&_svg]:hidden"
                >
                  <span
                    className="inline-flex items-center gap-1.5 min-w-0 max-w-full"
                    title={cmd.label}
                  >
                    <span className="font-mono font-semibold text-foreground truncate">
                      {cmd.label}
                    </span>
                    {cmd.isExpert && (
                      <span
                        className="shrink-0 rounded-sm bg-primary/10 px-1 text-[9px] font-semibold uppercase tracking-wider text-primary"
                        aria-label="Expert specialist"
                      >
                        expert
                      </span>
                    )}
                  </span>
                  <span
                    className="min-w-0 truncate text-sm text-muted-foreground"
                    title={cmd.description}
                  >
                    {cmd.description}
                  </span>
                </CommandItem>
              ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
