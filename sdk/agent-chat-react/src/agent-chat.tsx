import { useCallback, useEffect, useMemo, useState } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { BrowserPane } from "./components/browser/browser-pane";
import { useBrowserPreview } from "./components/browser/use-browser-preview";
import { ChatThread } from "./components/chat/chat-thread";
import { WhiteboardSurface } from "./components/whiteboard/agent-whiteboard";
import { TooltipProvider } from "./components/ui/tooltip";
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
}

// CSS variable controlling the desktop right-stack width. Inlined as a style
// so it stays component-local; arbitrary-value Tailwind classes read it.
const RIGHT_STACK_STYLE = {
  // Both the column's width and the chat panel's right offset read this, so
  // one value keeps them complementary. 50%: the browser shell renders a real
  // page, and page layouts are unusable in a 440px strip.
  ["--right-stack-w" as string]: "50%",
} as React.CSSProperties;

/** The panes the phone layout shows one at a time. */
type MobilePane = "chat" | "browser" | "workspace";

const MOBILE_PANE_LABELS: Record<MobilePane, string> = {
  chat: "Chat",
  browser: "Browser",
  workspace: "Workspace",
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
}: AgentChatProps) {
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
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

  const runtime = useAgentChatRuntime({
    adapter,
    agentId,
    sessionId,
    onSessionChange,
  });

  // Reset right-stack pane defaults when the user flips view modes.
  // Simple mode hides the workspace pane; Expert mode shows it.
  // Only fires on viewMode transitions, so manual toggles within a
  // mode aren't clobbered.
  useEffect(() => {
    // Switching modes closes the column rather than choosing a pane for
    // the user: the cards make re-opening one click.
    setOpenPane(null);
  }, [runtime.viewMode]);
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
  const workspaceAvailable = !!sessionId;
  const workspaceVisible = workspaceAvailable && openPane === "workspace";
  const rightStackVisible = browserVisible || workspaceVisible;

  // The card is visible whether or not the pane is, so its thumbnail comes
  // from the preview endpoint rather than the shell's live frames.
  const browserPreview = useBrowserPreview({
    adapter,
    sessionId,
    enabled: browserAvailable,
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
    if (openPane === "workspace" && !workspaceAvailable) setOpenPane(null);
  }, [openPane, browserAvailable, workspaceAvailable]);

  useEffect(() => {
    onMessagesChange?.(runtime.messages);
  }, [onMessagesChange, runtime.messages]);

  const handleFileSelect = useCallback(
    (path: string) => {
      setWorkspacePath(path);
      // Selecting a file on mobile should bring the workspace tab to the
      // front so the user can see the file they just opened. Also force
      // the workspace pane visible if the user had hidden it.
      setMobileView("workspace");
      setOpenPane("workspace");
      onFileSelect?.(path);
    },
    [onFileSelect],
  );

  const handleOpenBrowserPane = useCallback(() => {
    setOpenPane("browser");
    setMobileView("browser");
  }, []);

  const handleOpenWorkspacePane = useCallback(() => {
    setOpenPane("workspace");
    setMobileView("workspace");
  }, []);

  const handleToggleBrowser = useCallback(() => {
    setOpenPane((prev) => (prev === "browser" ? null : "browser"));
    setMobileView("browser");
  }, []);

  const handleToggleWorkspace = useCallback(() => {
    setOpenPane((prev) => (prev === "workspace" ? null : "workspace"));
    setMobileView("workspace");
  }, []);

  // The phone toggle offers exactly the panes that currently exist, so it
  // disappears when there is only the chat. `mobileView` is validated against
  // that list rather than trusted: a browser that closes, or a workspace that
  // goes away with the session, would otherwise leave the layout parked on a
  // tab that renders nothing.
  const mobilePanes = useMemo<MobilePane[]>(() => {
    const panes: MobilePane[] = ["chat"];
    if (browserVisible) panes.push("browser");
    if (workspaceVisible) panes.push("workspace");
    return panes;
  }, [browserVisible, workspaceVisible]);
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
            browserVisible
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
                "data-[mobile-view=browser]:hidden data-[mobile-view=workspace]:hidden md:flex!",
              // md+: positioning depends on whether the browser/workspace
              // right stack is laid out. When the browser pane is shown
              // we pin a fixed-width column on the right and absolutely
              // position the chat panel beside it. Otherwise the chat
              // panel just flexes alongside the workspace (or fills the
              // space when nothing else is visible).
              browserVisible
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
              onRetry={runtime.retry}
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
              showWorkspace={openPane === "workspace"}
              onToggleWorkspace={handleToggleWorkspace}
              canShowBrowser={browserAvailable}
              canShowWorkspace={workspaceAvailable}
              paneCards={{
                browser: browserRunning
                  ? {
                      subtitle: browserState?.controlOwner
                        ? `${browserState.controlOwner} has control`
                        : undefined,
                      thumbnail: browserPreview,
                      onOpen: handleOpenBrowserPane,
                    }
                  : null,
                files: workspaceAvailable
                  ? { onOpen: handleOpenWorkspacePane }
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
                // md+: positioning differs depending on what is inside.
                // - browser visible (with or without workspace): absolute
                //   right column at the configured width.
                // - workspace only: relative shrink-0 column letting
                //   WorkspacePanel manage its own width via the resize
                //   handle.
                browserVisible
                  ? "md:absolute md:inset-y-0 md:right-0 md:w-(--right-stack-w,440px) md:flex-none"
                  : "md:relative md:shrink-0 md:flex-none md:w-auto",
              )}
            >
              {browserVisible && (
                <div
                  data-testid="browser-panel"
                  data-mobile-view={activeMobilePane}
                  className="min-h-0 h-full w-full overflow-hidden"
                >
                  <BrowserPane
                    sessionId={sessionId}
                    state={browserState}
                    adapter={adapter}
                    onClose={() => setOpenPane(null)}
                  />
                </div>
              )}
              {workspaceVisible && (
                <div
                  data-testid="workspace-panel-frame"
                  data-mobile-view={activeMobilePane}
                  // No w-full: the right column is md:w-auto when the workspace
                  // is the open pane, so stretching here makes it swallow the
                  // whole width. WorkspacePanel sizes itself via its resize
                  // handle instead, which is what fillParent turns off.
                  className="min-h-0 h-full"
                >
                  <WorkspacePanel
                    adapter={adapter}
                    sessionId={sessionId}
                    selectedPath={workspacePath}
                    onSelectedPathChange={setWorkspacePath}
                    refreshSignal={runtime.workspaceRefreshKey}
                    disabled={effectiveDisabled}
                    fillParent={false}
                  />
                </div>
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
