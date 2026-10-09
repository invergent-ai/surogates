import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { useProjectThreads } from "./components/chat/use-project-threads";
import {
  BrowserPane,
  type ComputerBrowser,
  ComputerBrowserPane,
} from "./components/browser/browser-pane";
import { useBrowserPreview } from "./components/browser/use-browser-preview";
import { ChatThread } from "./components/chat/chat-thread";
import { WhiteboardSurface } from "./components/whiteboard/agent-whiteboard";
import { TooltipProvider } from "./components/ui/tooltip";
import { WorkspaceFileDrawer } from "./components/workspace/workspace-file-drawer";
import { WorkspacePanel } from "./components/workspace/workspace-panel";
import { cn } from "./lib/utils";
import {
  isSubAgentSession,
  readOnlyReasonForSession,
} from "./lib/sessions";
import { useAgentChatRuntime } from "./runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatMessage,
  AgentChatViewMode,
} from "./types";
import type { ChatComposerError } from "./components/chat/chat-composer";
import { isBoardSession } from "./components/whiteboard/persist";

export interface AgentChatProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  onFileSelect?: (path: string) => void;
  onMessagesChange?: (messages: AgentChatMessage[]) => void;
  /**
   * The host only reads this chat. Nothing in it writes: the composer and the file panel's
   * changes are off, and so are Stop, Retry, answers to the agent's questions, an expert's
   * rating, and the browser's card, its control and its close.
   */
  disabled?: boolean;
  /**
   * Called when the composer rejects a file selection before sending —
   * size/count caps, accept-pattern misses.  Host apps wire this to
   * their toast system; the SDK does not surface these on its own.
   */
  onComposerError?: (err: ChatComposerError) => void;
  /**
   * Browser-profile selection (host-managed). When ``onSelectBrowserProfile``
   * is provided and ``browserProfilesEnabled`` is set, the composer shows a
   * profile picker; the host threads the chosen id into session creation.
   * The picker is shown before a session exists (a profile can only be bound
   * at creation) and is locked once a session is active.
   */
  browserProfileId?: string | null;
  onSelectBrowserProfile?: (id: string | null) => void;
  /** Whether this agent supports a live browser (gates the profile picker). */
  browserProfilesEnabled?: boolean;
  /**
   * Per-agent capability flag.  When true, the composer surfaces the
   * ``/deep-research`` slash command in its builtin menu.  Off by
   * default; the host (Studio) reads it from the agent record and
   * passes it through.  Wired this way (not via the runtime) because
   * the SDK has no notion of the agent's settings -- the host owns
   * that domain.
   */
  deepResearchEnabled?: boolean;
  /**
   * When true, the composer surfaces the `/auto-research` slash command
   * (research missions / Arbor). Like `deepResearchEnabled`, the host owns
   * the capability gate.
   */
  researchEnabled?: boolean;
  /**
   * When true, the composer exposes the `/code` coding-agent slash commands.
   * Like `deepResearchEnabled`, the host owns the capability gate.
   */
  codeAgentsEnabled?: boolean;
  /**
   * Whether this agent may draw on a canvas.
   *
   * One half of the gate on the composer's Whiteboard segment; the other
   * is the session having been created as a board. Like
   * `deepResearchEnabled`, the host owns this half.
   */
  whiteboardEnabled?: boolean;
  /**
   * Slash-command capability group (per-agent). These gate the always-on
   * lightweight builtins and default to shown when omitted, so a host that
   * hasn't wired them keeps the current menu. `/clear` has no flag and is
   * always available; the host owns the capability gate.
   */
  loopsEnabled?: boolean;
  missionsEnabled?: boolean;
  goalsEnabled?: boolean;
  compressEnabled?: boolean;
  /**
   * Called when the user clicks the integrations band under the composer.
   * Hosts navigate to their Integrations route. When omitted, the band is
   * not rendered.
   */
  onOpenIntegrations?: () => void;
  /**
   * Navigate to the host's billing page. When provided, a 402
   * ``insufficient_credits`` failure renders a "buy credits / upgrade"
   * card with a "Go to Billing" button that calls this. The host owns the
   * billing route.
   */
  onOpenBilling?: () => void;
  /**
   * What the host says under the composer: where a new chat will work, as a
   * folder of the user's computer. Omitted, nothing is shown there.
   */
  composerFooter?: React.ReactNode;
  /**
   * A message the host has a new chat send as its first, as if its user had typed and sent it:
   * Surogate Desktop's quick entry hands one. Sent once per id, while the chat has no session and
   * is not disabled; it goes through the adapter's createSession as any first message does.
   */
  firstMessage?: { id: string; text: string } | null;
  /**
   * Told what became of the host's first message, by its id, once: null once it was sent, or why it
   * was not, as when a chat was already open, or the chat went before it could send it. The callback
   * the host passes by then is the one told.
   */
  onFirstMessageSent?: (id: string, error: string | null) => void;
  /**
   * The browser pane of a chat whose browser is on the user's computer, as the
   * host draws it: where it is, and what the host can do with it there.
   * *available* is false where that computer has no supported browser, and
   * *readOnly* is true in a chat the host only reads. Omitted, the pane says
   * where the browser is.
   */
  computerBrowser?: (browser: ComputerBrowser) => React.ReactNode;
}

// CSS variable controlling the desktop right-stack width. Inlined as a style
// so it stays component-local; arbitrary-value Tailwind classes read it.
const RIGHT_STACK_STYLE = {
  // Both the column's width and the chat panel's right offset read this, so
  // one value keeps them complementary. 50%: the browser shell renders a real
  // page, and page layouts are unusable in a 440px strip.
  ["--right-stack-w" as string]: "50%",
} as React.CSSProperties;

/** The panes the phone layout shows one at a time. The files *tree* is not
 *  one — it expands as an accordion inside the chat column at every width —
 *  but a file opened from it claims the right column as "file". */
type MobilePane = "chat" | "browser" | "file";

const MOBILE_PANE_LABELS: Record<MobilePane, string> = {
  chat: "Chat",
  browser: "Browser",
  file: "Preview",
};

export function AgentChat({
  adapter,
  agentId,
  sessionId,
  onSessionChange,
  onFileSelect,
  onMessagesChange,
  disabled,
  onComposerError,
  browserProfileId,
  onSelectBrowserProfile,
  browserProfilesEnabled = false,
  deepResearchEnabled = false,
  researchEnabled = false,
  codeAgentsEnabled = false,
  whiteboardEnabled = false,
  loopsEnabled = true,
  missionsEnabled = true,
  goalsEnabled = true,
  compressEnabled = true,
  onOpenIntegrations,
  onOpenBilling,
  composerFooter,
  firstMessage,
  onFirstMessageSent,
  computerBrowser,
}: AgentChatProps) {
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  // What the drawer shows — separate from the tree selection above, because
  // selecting a folder highlights it but must leave the open preview alone.
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  // On phones the chat, browser and workspace panes don't fit side-by-side. A
  // segmented control at the top of the layout swaps between them, one at a
  // time and full-height. On md+ they lay out together and the toggle is
  // hidden. Splitting the right stack 50/50 the way the desktop does would
  // give each pane ~200px on a phone, which is not a file tree or a browser
  // so much as a rumour of one.
  const [mobileView, setMobileView] = useState<MobilePane>("chat");
  // Which right-stack pane is open, if any. The column starts closed and
  // is opened from the cards above the composer; the composer's toggles
  // drive the same state, so the two affordances cannot disagree about
  // what is showing. One pane at a time -- the panes are tabs now, not
  // stacked halves.
  const [openPane, setOpenPane] = useState<MobilePane | null>(null);
  // The files accordion above the composer. Not a pane: it expands in place
  // inside the chat column, so it needs no phone tab and no drawer.
  const [filesOpen, setFilesOpen] = useState(false);

  const runtime = useAgentChatRuntime({
    adapter,
    agentId,
    sessionId,
    onSessionChange,
  });
  // A project's master: the server stamps its role and project into its config.
  const sessionConfig = runtime.session?.config;
  const projectId = sessionConfig?.workstream_role === "coordinator"
    && typeof sessionConfig.workstream_id === "string"
    ? sessionConfig.workstream_id
    : null;
  const threadRows = useProjectThreads(adapter, projectId);

  // Reset right-stack pane defaults when the user flips view modes.
  // Simple mode hides the workspace pane; Expert mode shows it.
  // Only fires on viewMode transitions, so manual toggles within a
  // mode aren't clobbered.
  useEffect(() => {
    // Switching modes closes the column rather than choosing a pane for
    // the user: the cards make re-opening one click.
    setOpenPane(null);
    setFilesOpen(false);
  }, [runtime.viewMode]);

  // A different session is a different workspace: fold the accordion and
  // drop the old session's selection and preview rather than carrying them
  // where they refer to nothing.
  useEffect(() => {
    setFilesOpen(false);
    setWorkspacePath(null);
    setPreviewPath(null);
    setOpenPane(null);
  }, [sessionId]);
  const readOnly = readOnlyReasonForSession(runtime.session);
  // The canvas view is offered only on a session that was created as a
  // board: the harness loads ``whiteboard_draw`` on the same stamp, so
  // anywhere else the segment would open a canvas the agent cannot draw
  // on. The agent capability is checked too, so revoking the board takes
  // it away from boards that already exist.
  const boardAvailable = whiteboardEnabled && isBoardSession(runtime.session);

  // A stored preference outlives both of those, and by then the segment
  // is gone -- the board would be a room with the door bricked up, so
  // fall back to the transcript.
  const viewMode: AgentChatViewMode =
    runtime.viewMode === "whiteboard" && !boardAvailable
      ? "simple"
      : runtime.viewMode;

  const effectiveDisabled = disabled || readOnly.readOnly;
  const disabledReason = readOnly.reason;
  // The TurnSummaryCard renders an LLM-generated recap of the just-
  // completed turn.  Suppress it on:
  //   * sub-agent sessions (already gated below) -- nobody is reading
  //     the recap in those, the parent's LLM polls the final result.
  //   * root sessions that orchestrate a deep-research workflow.  The
  //     base agent's "turn" there is a single ``delegate_task`` call;
  //     the final artifact IS the recap.  An extra summary card just
  //     repeats the work in a less useful form.
  //   * boards.  The canvas is the deliverable and it is already on
  //     screen, so the card's only content is ``_whiteboard/canvas.json``
  //     -- the board's own backing file, named with the ``_`` prefix
  //     precisely to keep it out of the user's way.
  const orchestratesDeepResearch = useMemo(
    () => runtime.messages.some(
      (m) => m.toolCalls?.some(
        (tc) => tc.toolName === "delegate_task"
          && delegateTaskTargets(tc.args).includes("deep-research"),
      ),
    ),
    [runtime.messages],
  );
  const hideTurnSummary =
    isSubAgentSession(runtime.session)
    || orchestratesDeepResearch
    || isBoardSession(runtime.session);
  const browserState = runtime.state.browser;
  // A "closed" browser state is functionally the same as no browser — the
  // BrowserPane would otherwise render an empty "preview unavailable" panel.
  const browserAvailable =
    browserState !== null && browserState.status !== "closed" && !!sessionId;
  const browserVisible = browserAvailable && openPane === "browser";
  // On the user's computer: its window is the live view, and the cloud has nothing to preview.
  const browserOnComputer = browserState?.computer === true;
  const workspaceAvailable = !!sessionId;
  // The file preview shares the browser pane's slot and geometry: a file is
  // a document to read, not a strip to squint at inside the accordion.
  const filePreviewVisible =
    workspaceAvailable && previewPath !== null && openPane === "file";
  const rightStackVisible = browserVisible || filePreviewVisible;

  // The card is visible whether or not the pane is, so its thumbnail comes
  // from the preview endpoint rather than the shell's live frames.
  const browserPreview = useBrowserPreview({
    adapter,
    sessionId,
    enabled: browserAvailable && !browserOnComputer,
  });
  // Session state says a browser exists; the preview says whether it really
  // does. Waiting for a confirmed yes rather than showing on "not yet known":
  // being optimistic meant a dead browser's card appeared and vanished within
  // a second, and a flash of a control is worse than showing it a beat late.
  // The card trusts session state, exactly as the old live view did:
  // browser.provisioned shows it, browser.destroyed / a 404 from
  // getBrowserState hides it. The registry self-heals server-side now, so
  // that state is honest; probing liveness from the client on top of it is
  // what caused the flashing and the missing-card bugs.
  const browserRunning = browserAvailable;

  // A pane that goes away while it is the open one must not leave the column
  // parked on nothing: the browser can be destroyed mid-session.
  useEffect(() => {
    if (openPane === "browser" && !browserAvailable) setOpenPane(null);
    if (openPane === "file" && (!workspaceAvailable || previewPath === null))
      setOpenPane(null);
  }, [openPane, browserAvailable, workspaceAvailable, previewPath]);

  useEffect(() => {
    onMessagesChange?.(runtime.messages);
  }, [onMessagesChange, runtime.messages]);

  // The host's first message, once: a drawing again sends it no more. It goes once this commit's
  // effects have run, StrictMode's second run of the runtime's reset included, which would wipe it
  // from the transcript; an effect cleaned up before then sends nothing. The host hears what became
  // of it at the callback it has by then, and hears of one that will never go: a chat already open
  // takes none, and a chat that is gone sends none.
  const firstSent = useRef<string | null>(null);
  const first = useRef({ message: firstMessage ?? null, told: onFirstMessageSent });
  useEffect(() => {
    first.current = { message: firstMessage ?? null, told: onFirstMessageSent };
  });
  const send = runtime.send;
  useEffect(() => {
    if (!firstMessage || firstSent.current === firstMessage.id) return;
    if (sessionId !== null) {
      firstSent.current = firstMessage.id;
      first.current.told?.(firstMessage.id, "Another chat opened before this one was made, so nothing was sent.");
      return;
    }
    if (effectiveDisabled) return;
    let live = true;
    queueMicrotask(() => {
      if (!live || firstSent.current === firstMessage.id) return;
      firstSent.current = firstMessage.id;
      // A send that fails is marked on its message too, as one from the composer is.
      send(firstMessage.text).then(
        () => first.current.told?.(firstMessage.id, null),
        (error: unknown) => first.current.told?.(firstMessage.id, error instanceof Error ? error.message : String(error)),
      );
    });
    return () => {
      live = false;
    };
  }, [firstMessage, sessionId, effectiveDisabled, send]);
  // Gone with one still to send: said once the chat has not come back, as after StrictMode's passing unmount it has.
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      queueMicrotask(() => {
        const { message, told } = first.current;
        if (mounted.current || !message || firstSent.current === message.id) return;
        firstSent.current = message.id;
        told?.(message.id, "The page left the new chat before it was made, so nothing was sent.");
      });
    };
  }, []);

  // One path for every "open this file" gesture — a tree row, a file chip in
  // the transcript — so they all land in the same drawer.
  const handleOpenFilePreview = useCallback((path: string) => {
    setWorkspacePath(path);
    setPreviewPath(path);
    setOpenPane("file");
    setMobileView("file");
  }, []);

  // The panel clears the selection when the selected file is deleted or the
  // session goes away — moments when the preview must not linger either.
  const handleSelectedPathChange = useCallback((path: string | null) => {
    setWorkspacePath(path);
    if (path === null) setPreviewPath(null);
  }, []);

  const handleFileSelect = useCallback(
    (path: string) => {
      handleOpenFilePreview(path);
      onFileSelect?.(path);
    },
    [handleOpenFilePreview, onFileSelect],
  );

  const handleToggleBrowser = useCallback(() => {
    setOpenPane((prev) => (prev === "browser" ? null : "browser"));
    setMobileView("browser");
  }, []);

  const handleToggleFiles = useCallback(() => {
    setFilesOpen((open) => !open);
  }, []);

  // The phone toggle offers exactly the panes that currently exist, so it
  // disappears when there is only the chat. `mobileView` is validated against
  // that list rather than trusted: a browser that closes, or a workspace that
  // goes away with the session, would otherwise leave the layout parked on a
  // tab that renders nothing.
  const mobilePanes = useMemo<MobilePane[]>(() => {
    const panes: MobilePane[] = ["chat"];
    if (browserVisible) panes.push("browser");
    if (filePreviewVisible) panes.push("file");
    return panes;
  }, [browserVisible, filePreviewVisible]);
  const activeMobilePane = mobilePanes.includes(mobileView)
    ? mobileView
    : "chat";
  const showMobileToggle = mobilePanes.length > 1;

  return (
    <AgentChatAdapterProvider
      value={{
        adapter,
        sessionId,
        onFileSelect: handleFileSelect,
        onOpenBilling,
        onOpenSession: onSessionChange,
        projectId,
        threadRows,
        readOnly: disabled === true,
      }}
    >
      <TooltipProvider>
        <section
          data-testid="agent-chat-layout"
          data-mobile-view={mobileView}
          className={cn(
            // Phone: flex column, tab toggle on top, then either chat or
            // right stack visible based on `data-mobile-view`.
            "flex min-h-0 flex-1 flex-col overflow-hidden bg-background text-sm text-foreground",
            // md+: restore desktop two-pane layout when the right stack
            // is visible. With both panes, absolute positioning lets the
            // browser/workspace split occupy a fixed width. Without the
            // right stack, the chat takes the full width.
            rightStackVisible
              ? "md:relative md:flex-row"
              : "md:flex-row",
          )}
          style={{ direction: "ltr", ...RIGHT_STACK_STYLE }}
        >
          {showMobileToggle && (
            <div
              data-testid="mobile-pane-toggle"
              className="md:hidden flex shrink-0 border-b border-line bg-card"
            >
              {mobilePanes.map((pane) => (
                <button
                  key={pane}
                  type="button"
                  onClick={() => setMobileView(pane)}
                  aria-pressed={activeMobilePane === pane}
                  className={cn(
                    "flex-1 min-h-11 px-4 py-3 text-sm font-medium border-b-2 -mb-px transition-colors",
                    activeMobilePane === pane
                      ? "border-primary text-foreground"
                      : "border-transparent text-subtle hover:text-foreground",
                  )}
                >
                  {MOBILE_PANE_LABELS[pane]}
                </button>
              ))}
            </div>
          )}

          <div
            data-testid="chat-panel"
            data-mobile-view={activeMobilePane}
            className={cn(
              // Phone: full width column, hidden while another pane is picked.
              "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
              showMobileToggle &&
                "data-[mobile-view=browser]:hidden data-[mobile-view=file]:hidden md:flex!",
              // md+: when the right column is laid out (browser pane or file
              // preview), pin it at the configured width and absolutely
              // position the chat panel beside it. Otherwise the chat panel
              // fills the space.
              rightStackVisible
                ? "md:absolute md:inset-y-0 md:left-0 md:right-(--right-stack-w,440px) md:flex"
                : "md:relative md:flex-1",
            )}
          >
            {viewMode === "whiteboard" ? (
              // The board replaces the transcript, on the same runtime
              // and the same session: switching view must not change
              // which conversation you are in.
              <WhiteboardSurface
                adapter={adapter}
                agentId={agentId}
                sessionId={sessionId}
                onSessionChange={onSessionChange}
                disabled={effectiveDisabled}
                runtime={runtime}
                viewMode={runtime.viewMode}
                onViewModeChange={runtime.setViewMode}
              />
            ) : (
            <ChatThread
              sessionId={sessionId}
              messages={runtime.messages}
              isRunning={runtime.isRunning}
              terminal={runtime.terminal}
              isLoadingHistory={runtime.isLoadingHistory}
              onSend={(content, images, attachments) =>
                runtime.send(content, images, attachments)
              }
              onStop={() => runtime.stop()}
              // A chat the host only reads is not run again from here.
              onRetry={disabled ? undefined : runtime.retry}
              onFileSelect={handleFileSelect}
              disabled={effectiveDisabled}
              disabledReason={disabledReason}
              tokenUsage={runtime.tokenUsage}
              retryIndicator={runtime.retryIndicator}
              onComposerError={onComposerError}
              browserProfileId={browserProfileId}
              onSelectBrowserProfile={onSelectBrowserProfile}
              browserProfilesEnabled={browserProfilesEnabled}
              browserProfileLocked={!!sessionId}
              showBrowser={openPane === "browser"}
              onToggleBrowser={handleToggleBrowser}
              showWorkspace={filesOpen}
              onToggleWorkspace={handleToggleFiles}
              canShowBrowser={browserAvailable}
              canShowWorkspace={workspaceAvailable}
              paneCards={{
                // A chat the host only reads offers its browser no card: the card is the way to take it over.
                browser: browserRunning && !disabled
                  ? {
                      subtitle: browserOnComputer
                        ? browserState?.status === "unavailable"
                          ? "No supported browser on its computer"
                          : "On the chat's computer"
                        : browserState?.controlOwner
                          ? `${browserState.controlOwner} has control`
                          : undefined,
                      thumbnail: browserPreview,
                      // Show and hide on the same card, like the Files
                      // accordion header and the composer's Tools item.
                      onOpen: handleToggleBrowser,
                    }
                  : null,
                files: workspaceAvailable
                  ? {
                      open: filesOpen,
                      onToggle: handleToggleFiles,
                      panel: (
                        <WorkspacePanel
                          adapter={adapter}
                          sessionId={sessionId}
                          selectedPath={workspacePath}
                          onSelectedPathChange={handleSelectedPathChange}
                          onOpenFile={handleOpenFilePreview}
                          refreshSignal={runtime.workspaceRefreshKey}
                          disabled={effectiveDisabled}
                        />
                      ),
                    }
                  : null,
              }}
              viewMode={viewMode}
              onViewModeChange={runtime.setViewMode}
              deepResearchEnabled={deepResearchEnabled}
              researchEnabled={researchEnabled}
              codeAgentsEnabled={codeAgentsEnabled}
              whiteboardEnabled={boardAvailable}
              loopsEnabled={loopsEnabled}
              missionsEnabled={missionsEnabled}
              goalsEnabled={goalsEnabled}
              compressEnabled={compressEnabled}
              researchSources={runtime.researchSources}
              hideTurnSummary={hideTurnSummary}
              agentId={agentId}
              onOpenIntegrations={onOpenIntegrations}
              deviceWait={runtime.state.deviceWait}
              composerFooter={composerFooter}
            />
            )}
          </div>
          {rightStackVisible && (
            <div
              data-testid="right-stack"
              data-mobile-view={activeMobilePane}
              className={cn(
                // Phone: full width column, hidden when chat tab active.
                "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
                showMobileToggle &&
                  "data-[mobile-view=chat]:hidden md:flex!",
                // md+: absolute right column at the configured width. The
                // browser is the only pane that lives here — files expand
                // as an accordion inside the chat column.
                "md:absolute md:inset-y-0 md:right-0 md:w-(--right-stack-w,440px) md:flex-none",
              )}
            >
              {browserVisible && (
                <div
                  data-testid="browser-panel"
                  data-mobile-view={activeMobilePane}
                  className="min-h-0 h-full w-full overflow-hidden"
                >
                  {browserOnComputer ? (
                    <ComputerBrowserPane
                      available={browserState.status !== "unavailable"}
                      readOnly={disabled === true}
                      draw={computerBrowser}
                    />
                  ) : (
                    <BrowserPane
                      sessionId={sessionId}
                      state={browserState}
                      adapter={adapter}
                      onClose={() => setOpenPane(null)}
                      readOnly={disabled === true}
                    />
                  )}
                </div>
              )}
              {filePreviewVisible && sessionId && previewPath && (
                <WorkspaceFileDrawer
                  adapter={adapter}
                  sessionId={sessionId}
                  path={previewPath}
                  onClose={() => setOpenPane(null)}
                />
              )}
            </div>
          )}
        </section>
      </TooltipProvider>
    </AgentChatAdapterProvider>
  );
}

// Extract every ``agent_type`` referenced by a ``delegate_task`` tool
// call's serialized args (either ``goal``+``agent_type`` or the
// batched ``goals: [...]`` form).  Returns ``[]`` when the args are
// not valid JSON so a partial-streamed tool call doesn't crash the
// memoised deep-research check.
function delegateTaskTargets(rawArgs: string): string[] {
  if (!rawArgs) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawArgs);
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== "object") return [];
  const out: string[] = [];
  const a = parsed as { agent_type?: unknown; goals?: unknown };
  if (typeof a.agent_type === "string") out.push(a.agent_type);
  if (Array.isArray(a.goals)) {
    for (const g of a.goals) {
      if (g && typeof g === "object" && typeof (g as { agent_type?: unknown }).agent_type === "string") {
        out.push((g as { agent_type: string }).agent_type);
      }
    }
  }
  return out;
}
