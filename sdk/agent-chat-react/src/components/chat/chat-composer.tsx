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
  ClockIcon,
  CloudIcon,
  FolderIcon,
  GlobeIcon,
  HardDriveIcon,
  IdCardIcon,
  ListTreeIcon,
  PenLineIcon,
  MessageSquareIcon,
  PaperclipIcon,
  PlusIcon,
  ShrinkIcon,
  SparklesIcon,
  TerminalIcon,
  type LucideIcon,
} from "lucide-react";
import type { PromptInputMessage } from "../ai-elements/prompt-input";
import { useProviderAttachments } from "../ai-elements/prompt-input";
import type {
  AgentChatBrowserProfile,
  AgentChatImageAttachment,
  AgentChatPendingAttachment,
  AgentChatSlashCommand,
  AgentChatViewMode,
  TokenUsage,
} from "../../types";
import { useAgentChatAdapterContext } from "../../adapter-context";
import { splitComposerFiles } from "../../lib/split-composer-files";
import {
  ContextContentBody,
  ContextContentHeader,
  ContextIcon,
  ContextValueProvider,
} from "../ai-elements/context";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";
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
import { Popover, PopoverAnchor } from "../ui/popover";
import {
  Command,
  CommandGroup,
  CommandItem,
  CommandList,
} from "../ui/command";
import { ResponsivePanel } from "../ui/responsive-panel";
import {
  ComposerCommandMenu,
  type ComposerMenuMode,
} from "./composer-command-menu";

// ── Slash command entry ──────────────────────────────────────────────

type SlashCommand = AgentChatSlashCommand;

// A row in the composer tools panel. 44px on touch — these are the panel's
// only targets, and in a bottom sheet they are what the thumb lands on.
const TOOLS_ITEM_CLASS =
  "gap-3 rounded-md px-3 py-2 pointer-coarse:min-h-11 pointer-coarse:text-[15px]";

// The view-mode switch, in the order the segments are shown.
const VIEW_MODE_SEGMENTS = [
  {
    mode: "simple" as const,
    label: "Simple",
    icon: MessageSquareIcon,
    tooltip: "Simple — just the conversation",
  },
  {
    mode: "expert" as const,
    label: "Advanced",
    icon: ListTreeIcon,
    tooltip: "Advanced — every step the agent took",
  },
  {
    mode: "whiteboard" as const,
    label: "Whiteboard",
    icon: PenLineIcon,
    tooltip: "Whiteboard — sketch with it on a shared canvas",
  },
];

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
  /**
   * The agent asked a question and is waiting on the reply.  The turn
   * is still running, but the composer must send rather than stop — the
   * server converts the typed message into the answer.
   */
  awaitingAnswer?: boolean;
  /** Overrides the idle placeholder (e.g. while answering a question). */
  placeholder?: string;
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
  /** Currently-selected browser profile id (drives the active style). */
  browserProfileId?: string | null;
  /** When provided (with ``browserProfilesEnabled``), renders the profile selector. */
  onSelectBrowserProfile?: (id: string | null) => void;
  /**
   * When true, the browser-profile selector is shown. Unlike ``canShowBrowser``
   * (which is only true once a browser pane is live) this is a static agent
   * capability, so the selector is available *before* a session starts — the
   * only point at which a profile can actually be bound to the session.
   */
  browserProfilesEnabled?: boolean;
  /**
   * When true, the profile choice is locked: a session is already active, so
   * the selector renders disabled with an explanatory tooltip. A profile is
   * bound at session creation and cannot be changed mid-session.
   */
  browserProfileLocked?: boolean;
  /** When false (default), the workspace toggle button is omitted entirely. */
  canShowWorkspace?: boolean;

  // ── Simple/Expert view-mode toggle ───────────────────────────────
  // Optional. When ``onViewModeChange`` is provided, a two-segment
  // Simple/Expert toggle is rendered in the tools row.
  viewMode?: AgentChatViewMode;
  onViewModeChange?: (mode: AgentChatViewMode) => void;

  // ── Per-agent capabilities ───────────────────────────────────────
  // When true, the composer surfaces the ``/deep-research`` slash
  // command in its builtin menu.  Gated because the deep-research
  // sub-agents are only present in the published bundle when the
  // agent has the workflow toggled on; offering the command without
  // the bundle would dispatch a delegate_task to a missing agent
  // type.
  deepResearchEnabled?: boolean;
  // When true, the composer surfaces the ``/auto-research`` slash command
  // (research missions / Arbor) in its builtin menu. Gated like
  // ``deepResearchEnabled`` because the arbor-executor sub-agent is only in
  // the published bundle when the agent has the workflow toggled on.
  researchEnabled?: boolean;
  // When true, the composer exposes the ``/code`` coding-agent commands
  // in its builtin menu. Gated like ``deepResearchEnabled`` because the
  // host owns the capability.
  codeAgentsEnabled?: boolean;
  // ── Slash-command capability group (per-agent) ───────────────────
  // These gate the always-on lightweight builtins; unlike the opt-in
  // flags above they default to shown (``!== false``) so a host that
  // hasn't wired them yet keeps the current menu.  ``/clear`` has no
  // flag and is always available.
  loopsEnabled?: boolean;
  missionsEnabled?: boolean;
  goalsEnabled?: boolean;
  compressEnabled?: boolean;
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

// ── Expert-mode menu trigger ─────────────────────────────────────────
//
// The three buttons that open the slash menu in a pre-selected scope are
// identical bar their icon, label and target mode, so they share one
// presentational button.

function ComposerMenuButton({
  icon: Icon,
  label,
  onClick,
}: {
  icon: LucideIcon;
  label: string;
  onClick: () => void;
}) {
  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      className="rounded-sm -uppercase font-display bg-white dark:bg-accent border-2 border-accent cursor-pointer"
      onClick={onClick}
    >
      <Icon />
      {label}
    </Button>
  );
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
  awaitingAnswer = false,
  placeholder,
  tokenUsage,
  onComposerError,
  showBrowser = false,
  onToggleBrowser,
  showWorkspace = false,
  onToggleWorkspace,
  canShowBrowser = false,
  browserProfileId,
  onSelectBrowserProfile,
  browserProfilesEnabled = false,
  browserProfileLocked = false,
  canShowWorkspace = false,
  deepResearchEnabled = false,
  researchEnabled = false,
  codeAgentsEnabled = false,
  loopsEnabled = true,
  missionsEnabled = true,
  goalsEnabled = true,
  compressEnabled = true,
  viewMode = "simple",
  onViewModeChange,
}: ChatComposerProps) {
  const { adapter } = useAgentChatAdapterContext();
  // null = never loaded. Failures keep the last known list — blanking
  // it would erase the trigger's active-profile name over a transient
  // error.
  const [browserProfiles, setBrowserProfiles] = useState<
    AgentChatBrowserProfile[] | null
  >(null);
  const loadBrowserProfiles = useCallback(() => {
    if (!adapter.listBrowserProfiles) return;
    void adapter.listBrowserProfiles().then(setBrowserProfiles).catch(() => {});
  }, [adapter]);
  // The trigger names the active profile, so resolve the list as soon
  // as a profile id is present; the menu also refreshes on each open
  // (see onOpenChange) so a freshly-created profile appears without a
  // reload.
  useEffect(() => {
    if (!browserProfilesEnabled || !browserProfileId) return;
    if (browserProfiles !== null) return;
    loadBrowserProfiles();
  }, [browserProfilesEnabled, browserProfileId, browserProfiles, loadBrowserProfiles]);
  const activeBrowserProfile =
    (browserProfiles ?? []).find((p) => p.id === browserProfileId) ?? null;
  const { textInput, attachments } = usePromptInputController();
  // While the agent is parked on a question, the turn is technically
  // still running but the user is the one being waited on: the composer
  // must read as "reply", not "stop the agent".
  const status =
    isRunning && !awaitingAnswer ? "streaming" : disabled ? "error" : "ready";

  // ── Load skills from backend ─────────────────────────────────────

  const [adapterCommands, setAdapterCommands] = useState<SlashCommand[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [buttonMenuOpen, setButtonMenuOpen] = useState(false);
  const [menuMode, setMenuMode] = useState<ComposerMenuMode>("all");
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [menuDismissed, setMenuDismissed] = useState(false);
  // While the agent is parked on a question, the composer is an answer
  // field. Slash commands route around sendMessage entirely (/goal goes
  // to defineOutcome), so the question would stay open and unanswered
  // until it times out.
  const showSlashMenu =
    !menuDismissed &&
    !awaitingAnswer &&
    (textInput.value.startsWith("/") || buttonMenuOpen);

  // Re-open when user types a new `/` after dismissal.
  useEffect(() => {
    if (menuDismissed && !textInput.value.startsWith("/") && !buttonMenuOpen) {
      setMenuDismissed(false);
    }
  }, [buttonMenuOpen, menuDismissed, textInput.value]);

  // Slash typing shows the combined commands + skills menu. Only when
  // the menu was opened by typing, though: a button-opened menu has
  // already picked its scope (commands / skills / scheduled), and the
  // controlled Commands input mirrors its query back into the textarea
  // as `/<query>` — without this guard that mirror write would trip the
  // leading-`/` check and silently widen a Commands search to "all",
  // leaking skills into the results.
  useEffect(() => {
    if (!buttonMenuOpen && textInput.value.startsWith("/")) {
      setMenuMode("all");
    }
  }, [buttonMenuOpen, textInput.value]);

  // Re-fetch app-provided skills each time the menu opens with skills in scope.
  // Platform built-ins (docx, pdf, pptx, xlsx, kanban, …) are dropped here so
  // the slash menu lists only the skills a tenant authored or attached.
  useEffect(() => {
    if (!(showSlashMenu && (menuMode === "skills" || menuMode === "all"))) {
      return;
    }
    const pending = adapter.listSlashCommands?.();
    if (!pending) return;
    let cancelled = false;
    setSkillsLoading(true);
    pending
      .then((commands) => {
        if (!cancelled) {
          setAdapterCommands(commands.filter((c) => !c.isBuiltin));
        }
      })
      .catch(() => { /* best-effort */ })
      .finally(() => {
        if (!cancelled) setSkillsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [adapter, showSlashMenu, menuMode]);

  const builtinCommands = useMemo<SlashCommand[]>(
    () => {
      // /clear has no per-command flag and is always available.
      const base: SlashCommand[] = [
        { value: "/clear", label: "/clear", description: "Clear conversation" },
      ];
      // The lightweight builtins default to shown; only an explicit
      // ``false`` (the agent toggled the command off) hides them.
      if (compressEnabled) {
        base.push(
          { value: "/compress", label: "/compress", description: "Compress context" },
        );
      }
      if (goalsEnabled) {
        base.push(
          { value: "/goal", label: "/goal", description: "Define an outcome goal" },
          { value: "/goal status", label: "/goal status", description: "Show outcome goal status" },
          { value: "/goal pause", label: "/goal pause", description: "Pause automatic goal continuation" },
          { value: "/goal resume", label: "/goal resume", description: "Resume a paused goal" },
          { value: "/goal clear", label: "/goal clear", description: "Clear the current goal" },
        );
      }
      if (missionsEnabled) {
        base.push(
          { value: "/mission ", label: "/mission", description: "Start an orchestrated rubric-judged mission" },
          { value: "/mission status", label: "/mission status", description: "Show current mission status" },
          { value: "/mission pause", label: "/mission pause", description: "Pause the mission evaluator" },
          { value: "/mission resume", label: "/mission resume", description: "Resume a paused mission" },
          { value: "/mission cancel", label: "/mission cancel", description: "Cancel the mission" },
        );
      }
      if (loopsEnabled) {
        base.push(
          { value: "/loop", label: "/loop", description: "Schedule recurring prompt" },
          { value: "/loop list", label: "/loop list", description: "List active loops" },
          { value: "/loop cancel", label: "/loop cancel", description: "Cancel a loop by ID" },
        );
      }
      if (deepResearchEnabled) {
        // Trailing space so the user lands on the topic, not the
        // command name -- same pattern as "/mission ".
        base.push({
          value: "/deep-research ",
          label: "/deep-research",
          description: "Delegate a topic to the deep-research workflow",
        });
      }
      if (researchEnabled) {
        // Trailing space so the user lands on the goal, not the command
        // name -- same pattern as "/mission ". Only the builtin
        // /auto-research is surfaced; the arbor-research intake skill is
        // reached through normal skill autocomplete when attached.
        base.push({
          value: "/auto-research ",
          label: "/auto-research",
          description: "Launch an autonomous research mission (Arbor)",
        });
      }
      if (codeAgentsEnabled) {
        // Trailing space on the prompt-taking entries so the user lands
        // on the prompt, not the command name -- same pattern as "/mission ".
        base.push(
          { value: "/code claude ", label: "/code claude", description: "Run Claude Code on the workspace (your plan)" },
          { value: "/code codex ", label: "/code codex", description: "Run Codex on the workspace (your plan)" },
          { value: "/code status", label: "/code status", description: "Show connected coding agents" },
          { value: "/code login claude", label: "/code login claude", description: "Connect your Claude plan" },
          { value: "/code login codex", label: "/code login codex", description: "Connect your ChatGPT plan" },
        );
      }
      return base;
    },
    [
      compressEnabled,
      goalsEnabled,
      missionsEnabled,
      loopsEnabled,
      deepResearchEnabled,
      researchEnabled,
      codeAgentsEnabled,
    ],
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

  const openMenu = useCallback((mode: ComposerMenuMode) => {
    setMenuMode(mode);
    setMenuDismissed(false);
    setButtonMenuOpen(true);
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

      // Split incoming files into the inline-vision image list and the
      // workspace-upload pending list. Images appear in BOTH: the base64
      // data URL still drives inline vision, and the same image is queued
      // as a pending attachment so the runtime persists it to the
      // workspace ``uploads/`` directory, exactly like a non-image file.
      // Everything else becomes a pending attachment only.
      const { images, pending } = splitComposerFiles(message.files);

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

      // Send straight through even while the agent is working: the
      // harness folds a mid-turn user.message into the running wake at
      // the next iteration boundary, so a correction lands without
      // discarding the turn. Stopping first would pause the session,
      // compensate its sagas and destroy the sandbox pod — a cold
      // restart to deliver one sentence. The Stop button remains the
      // way to actually abort.
      await onSend(
        text,
        images.length > 0 ? images : undefined,
        pending.length > 0 ? pending : undefined,
      );
    },
    [onSend, onComposerError, disabled],
  );

  const handlePromptInputError = useCallback(
    (err: { code: "max_files" | "max_file_size" | "accept"; message: string }) => {
      onComposerError?.({ code: err.code, message: err.message });
    },
    [onComposerError],
  );

  // ── Render ───────────────────────────────────────────────────────

  // Two icons in the tools row, beside add / browser profile / workspace /
  // context — which is where a control that changes what the thread shows
  // belongs. It was a pair of text buttons next to Send: ~120px of chrome
  // reading "Simple Advanced" beside the one control you press every message,
  // and wide enough that the footer wrapped on a phone and stacked the tools
  // into a second toolbar. The labels move to tooltips and screen readers.
  const [contextOpen, setContextOpen] = useState(false);
  // Whole percent: the popover header already prints the exact figure, and a
  // decimal on a button this size is noise.
  const contextPercentLabel = tokenUsage?.contextWindow
    ? `${Math.min(100, Math.round((tokenUsage.totalTokens / tokenUsage.contextWindow) * 100))}%`
    : "0%";

  // Input and output are the shape of every turn, so they are listed even at
  // zero. Reasoning and cache are model-dependent — a zero row for a model
  // that has neither is a line that says nothing.
  const contextBreakdown = tokenUsage
    ? [
        { label: "Input", tokens: tokenUsage.inputTokens },
        { label: "Output", tokens: tokenUsage.outputTokens },
        ...(tokenUsage.reasoningTokens > 0
          ? [{ label: "Reasoning", tokens: tokenUsage.reasoningTokens }]
          : []),
        ...(tokenUsage.cachedInputTokens > 0
          ? [{ label: "Cache", tokens: tokenUsage.cachedInputTokens }]
          : []),
      ]
    : [];

  // ── What the tools panel contains ──────────────────────────────────
  //
  // Attach, the pane toggles and the profile picker were four buttons in a
  // row that a phone has no width for. They are one panel now, but their
  // gates stay independent — folding them together must not make any of them
  // appear where it did not, or disappear where it did.
  //
  // Attachments are new material for the next turn: the server refuses to
  // read them as the pending answer (see _resolve_pending_question), so
  // offering them while a question is open would park it until it times out.
  // The panes and the profile have no such constraint.
  const canAttach = !disabled && !awaitingAnswer;
  const showPaneToggles = Boolean(
    (canShowBrowser && onToggleBrowser) || (canShowWorkspace && onToggleWorkspace),
  );
  const canPickProfile = Boolean(
    browserProfilesEnabled &&
      onSelectBrowserProfile &&
      adapter.listBrowserProfiles,
  );
  // With nothing to put in it the button itself is noise — and a read-only or
  // awaiting-answer session with no panes has exactly nothing.
  const showToolsPanel = canAttach || showPaneToggles || canPickProfile;

  // A segmented control, not two buttons that happen to touch: one track,
  // two segments, only the selected one filled. The words are the label a
  // cursor gets — they read at a glance and cost nothing at that width. A
  // phone has no room for them beside Send, so there the icons stand in and
  // the words move to the accessible name (which both widths carry anyway).
  //
  // Plain <div> rather than ButtonGroup: the group squares off the inner
  // edges of every child, which fights the pill and leaves each segment
  // outlined inside an outline.
  const viewModeToggle = onViewModeChange ? (
    <div
      role="group"
      aria-label="Chat view mode"
      className="flex shrink-0 items-center gap-0.5 rounded-full bg-muted/60 p-0.5"
    >
      {VIEW_MODE_SEGMENTS.map(({ mode, label, icon: Icon, tooltip }) => {
        const selected = viewMode === mode;
        return (
          <PromptInputButton
            key={mode}
            size="sm"
            aria-label={`${label} view`}
            aria-pressed={selected}
            tooltip={tooltip}
            variant="ghost"
            onClick={() => onViewModeChange(mode)}
            className={cn(
              // Icon-width on a phone, word-width with a cursor; the coarse
              // bump takes the segment to the 44px touch floor either way.
              // Words, not the shouted uppercase the button base defaults to —
              // this is a label on a switch, not a call to action.
              "h-8 gap-1.5 rounded-full border-0 bg-transparent px-3.5 text-xs font-normal capitalize tracking-normal md:px-3 pointer-coarse:h-11 pointer-coarse:px-5",
              selected
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
            )}
          >
            <Icon className="size-4 md:hidden" />
            <span className="hidden md:inline">{label}</span>
          </PromptInputButton>
        );
      })}
    </div>
  ) : null;

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
      {viewMode === "expert" && !disabled && (
        <div className="flex flex-wrap items-center justify-end gap-2 px-1 pb-2">
          <ComposerMenuButton
            icon={TerminalIcon}
            label="Commands"
            onClick={() => openMenu("commands")}
          />
          <ComposerMenuButton
            icon={SparklesIcon}
            label="Skills"
            onClick={() => openMenu("skills")}
          />
          {loopsEnabled && (
            <ComposerMenuButton
              icon={ClockIcon}
              label="Scheduled Tasks"
              onClick={() => openMenu("scheduled")}
            />
          )}
        </div>
      )}
      <PopoverAnchor asChild>
        <PromptInput
          onSubmit={handleSubmit}
          multiple
          maxFiles={MAX_IMAGES_PER_MESSAGE + MAX_ATTACHMENTS_PER_MESSAGE}
          maxFileSize={MAX_ATTACHMENT_BYTES}
          onError={handlePromptInputError}
          beamActive={status === "streaming"}
        >
          <PromptInputBody>
            <PromptInputTextarea
              ref={textareaRef}
              placeholder={
                disabled
                  ? disabledReason ?? "Session disabled"
                  : placeholder ?? "Send a message..."
              }
              disabled={disabled}
            />
          </PromptInputBody>
          <PromptInputFooter>
            <PromptInputTools>
              {showToolsPanel && (
                <ResponsivePanel
                  open={addMenuOpen}
                  onOpenChange={(open) => {
                    setAddMenuOpen(open);
                    // The profile list is fetched lazily; the panel that shows
                    // it is now this one.
                    if (
                      open &&
                      canPickProfile &&
                      !browserProfileLocked &&
                      browserProfiles === null
                    ) {
                      loadBrowserProfiles();
                    }
                  }}
                  title="Composer tools"
                  trigger={
                    <PromptInputButton
                      aria-label="Composer tools"
                      tooltip="Attach files, panes and browser profile"
                    >
                      <PlusIcon className="size-4" />
                    </PromptInputButton>
                  }
                >
                  <Command>
                    <CommandList className="max-md:max-h-none">
                      {canAttach && (
                        <CommandGroup heading="Attach">
                          <CommandItem
                            onSelect={() => {
                              setAddMenuOpen(false);
                              attachments.openFileDialog();
                            }}
                            className={TOOLS_ITEM_CLASS}
                          >
                            <PaperclipIcon className="size-4 shrink-0 text-muted-foreground" />
                            Add local files
                          </CommandItem>
                          <CommandItem disabled className={TOOLS_ITEM_CLASS}>
                            <CloudIcon className="size-4 shrink-0 text-sky-500" />
                            Add from OneDrive
                          </CommandItem>
                          <CommandItem disabled className={TOOLS_ITEM_CLASS}>
                            <HardDriveIcon className="size-4 shrink-0 text-emerald-500" />
                            Add from Google Drive
                          </CommandItem>
                        </CommandGroup>
                      )}
                      {showPaneToggles && (
                        // Panes stay available while a question is pending —
                        // only attachments are suppressed there.
                        <CommandGroup heading="Panes">
                          {canShowBrowser && onToggleBrowser && (
                            <CommandItem
                              onSelect={onToggleBrowser}
                              data-checked={showBrowser || undefined}
                              className={TOOLS_ITEM_CLASS}
                            >
                              <GlobeIcon className="size-4 shrink-0 text-muted-foreground" />
                              Browser
                            </CommandItem>
                          )}
                          {canShowWorkspace && onToggleWorkspace && (
                            <CommandItem
                              onSelect={onToggleWorkspace}
                              data-checked={showWorkspace || undefined}
                              className={TOOLS_ITEM_CLASS}
                            >
                              <FolderIcon className="size-4 shrink-0 text-muted-foreground" />
                              Workspace
                            </CommandItem>
                          )}
                        </CommandGroup>
                      )}
                      {canPickProfile && (
                        <CommandGroup heading="Browser profile">
                          {browserProfileLocked ? (
                            // A profile is bound at session creation and cannot
                            // change mid-session. In a menu there is nothing to
                            // hover, so the reason is stated outright rather
                            // than hidden in a tooltip as it was on the button.
                            <>
                              <CommandItem
                                disabled
                                data-checked={!!browserProfileId || undefined}
                                className={TOOLS_ITEM_CLASS}
                              >
                                <IdCardIcon className="size-4 shrink-0 text-muted-foreground" />
                                {activeBrowserProfile?.name ?? "No profile"}
                              </CommandItem>
                              <p className="px-3 pt-0.5 pb-1.5 text-xs text-muted-foreground">
                                {activeBrowserProfile
                                  ? "Locked for this session."
                                  : "A profile can only be chosen before the session starts."}
                              </p>
                            </>
                          ) : (
                            // data-checked lights CommandItem's built-in
                            // trailing check on the selected entry.
                            [
                              { id: null as string | null, name: "No profile" },
                              ...(browserProfiles ?? []),
                            ].map((p) => (
                              <CommandItem
                                key={p.id ?? "none"}
                                onSelect={() => {
                                  setAddMenuOpen(false);
                                  onSelectBrowserProfile?.(p.id);
                                }}
                                data-checked={
                                  (browserProfileId ?? null) === p.id ||
                                  undefined
                                }
                                className={TOOLS_ITEM_CLASS}
                              >
                                <IdCardIcon className="size-4 shrink-0 text-muted-foreground" />
                                {p.name}
                              </CommandItem>
                            ))
                          )}
                        </CommandGroup>
                      )}
                    </CommandList>
                  </Command>
                </ResponsivePanel>
              )}
              {!disabled && tokenUsage && tokenUsage.contextWindow > 0 && (
                <ContextValueProvider
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
                  <ResponsivePanel
                    open={contextOpen}
                    onOpenChange={setContextOpen}
                    title="Session context"
                    align="end"
                    popoverClassName="min-w-60 divide-y p-0"
                    trigger={
                      <PromptInputButton
                        aria-label={`Session context: ${contextPercentLabel} of the window used`}
                        tooltip="Session context"
                        // This reports rather than commands, so it is styled
                        // as a readout: no box, no fill, muted until hovered.
                        // The bordered button it used to be carried the same
                        // weight as Send while saying far less. It keeps a
                        // full-height hit area — padding, not bulk.
                        variant="ghost"
                        className="h-9 gap-1 rounded-md border-0 bg-transparent px-1.5 text-muted-foreground hover:bg-accent/60 hover:text-foreground pointer-coarse:h-11"
                      >
                        <ContextIcon className="size-4" />
                        <span className="text-[11px] tabular-nums">
                          {contextPercentLabel}
                        </span>
                      </PromptInputButton>
                    }
                  >
                    <ContextContentHeader />
                    <ContextContentBody>
                      {tokenUsage.totalTokens > 0 ? (
                        contextBreakdown.map(({ label, tokens }) => (
                          <div
                            key={label}
                            className="flex items-center justify-between gap-4 text-xs"
                          >
                            <span className="text-muted-foreground">{label}</span>
                            <span className="tabular-nums">
                              {tokens.toLocaleString()}
                            </span>
                          </div>
                        ))
                      ) : (
                        <p className="text-xs text-muted-foreground text-center py-1">Empty</p>
                      )}
                    </ContextContentBody>
                    {tokenUsage.totalTokens > 0 && (
                      // The action the panel exists for, so it gets the panel's
                      // width rather than a filled block shoved into the corner
                      // of a grey bar — that bar was a second surface inside a
                      // small popover, squaring its own corners off against the
                      // rounded ones around it. It keeps a rule of its own
                      // because the sheet form has no divide-y, and the one
                      // division worth drawing is between reading and acting.
                      <div className="space-y-1.5 border-t border-border px-3 py-2.5">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="w-full"
                          onClick={() => {
                            // The sheet does not close itself on a plain
                            // button the way it does on a menu selection.
                            setContextOpen(false);
                            onSend("/compress");
                          }}
                        >
                          <ShrinkIcon />
                          Compress
                        </Button>
                        <p className="text-center text-[11px] leading-snug text-muted-foreground">
                          Sums up the thread to free the window
                        </p>
                      </div>
                    )}
                  </ResponsivePanel>
                </ContextValueProvider>
              )}
            </PromptInputTools>
            {/* ml-auto keeps this group right-aligned once the footer wraps,
                where justify-between no longer applies to a lone line. */}
            <div className="ml-auto flex shrink-0 items-center gap-2">
              {viewModeToggle}
              {!disabled && (
                <PromptInputSubmit
                  status={status}
                  onStop={onStop}
                  className="size-10"
                />
              )}
            </div>
          </PromptInputFooter>
        </PromptInput>
      </PopoverAnchor>
      <ComposerCommandMenu
        menuMode={menuMode}
        searchQuery={searchQuery}
        onSearchChange={handleSearchChange}
        skillsLoading={skillsLoading}
        builtinCommands={builtinCommands}
        adapterCommands={adapterCommands}
        scheduledExamples={scheduledExamples}
        onCommandSelect={handleCommandSelect}
        onEscapeDismiss={focusTextareaAtEnd}
      />
    </Popover>
  );
}
