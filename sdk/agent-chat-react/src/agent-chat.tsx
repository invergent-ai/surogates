import { useCallback, useEffect, useMemo, useState } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { BrowserPane } from "./components/browser/browser-pane";
import { ChatThread } from "./components/chat/chat-thread";
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
} from "./types";
import type { ChatComposerError } from "./components/chat/chat-composer";

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
  ["--right-stack-w" as string]: "440px",
} as React.CSSProperties;

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
  loopsEnabled = true,
  missionsEnabled = true,
  goalsEnabled = true,
  compressEnabled = true,
  onOpenIntegrations,
  onOpenBilling,
}: AgentChatProps) {
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  // On phones the chat and workspace panes don't fit side-by-side. A
  // segmented control at the top of the layout swaps between them. On md+
  // both are visible and the toggle is hidden.
  const [mobileView, setMobileView] = useState<"chat" | "workspace">("chat");
  // User-controlled visibility for the desktop right-stack panes.
  // Expert mode defaults both visible (the long-standing behavior);
  // Simple mode hides the workspace pane so the conversation stays
  // the centerpiece. The composer's toggle buttons still flip both
  // in either mode, and a manual toggle is preserved across renders
  // (the mode-sync effect below only fires when viewMode actually
  // changes).
  const [showBrowser, setShowBrowser] = useState(true);
  const [showWorkspace, setShowWorkspace] = useState(true);

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
    setShowWorkspace(runtime.viewMode === "expert");
  }, [runtime.viewMode]);
  const readOnly = readOnlyReasonForSession(runtime.session);
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
    isSubAgentSession(runtime.session) || orchestratesDeepResearch;
  const browserState = runtime.state.browser;
  // A "closed" browser state is functionally the same as no browser — the
  // BrowserPane would otherwise render an empty "preview unavailable" panel.
  const browserAvailable =
    browserState !== null && browserState.status !== "closed" && !!sessionId;
  const browserVisible = browserAvailable && showBrowser;
  const workspaceAvailable = !!sessionId;
  const workspaceVisible = workspaceAvailable && showWorkspace;
  const rightStackVisible = browserVisible || workspaceVisible;
  const bothPanesVisible = browserVisible && workspaceVisible;

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
      setShowWorkspace(true);
      onFileSelect?.(path);
    },
    [onFileSelect],
  );

  const handleToggleBrowser = useCallback(() => {
    setShowBrowser((prev) => !prev);
  }, []);

  const handleToggleWorkspace = useCallback(() => {
    setShowWorkspace((prev) => !prev);
  }, []);

  // Mobile toggle only makes sense if the right stack has something to show.
  const showMobileToggle = rightStackVisible;

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
            <div className="md:hidden flex shrink-0 border-b border-line bg-card">
              <button
                type="button"
                onClick={() => setMobileView("chat")}
                aria-pressed={mobileView === "chat"}
                className={cn(
                  "flex-1 px-4 py-3 text-sm font-medium border-b-2 -mb-px transition-colors",
                  mobileView === "chat"
                    ? "border-primary text-foreground"
                    : "border-transparent text-subtle hover:text-foreground",
                )}
              >
                Chat
              </button>
              <button
                type="button"
                onClick={() => setMobileView("workspace")}
                aria-pressed={mobileView === "workspace"}
                className={cn(
                  "flex-1 px-4 py-3 text-sm font-medium border-b-2 -mb-px transition-colors",
                  mobileView === "workspace"
                    ? "border-primary text-foreground"
                    : "border-transparent text-subtle hover:text-foreground",
                )}
              >
                Workspace
              </button>
            </div>
          )}

          <div
            data-testid="chat-panel"
            data-mobile-view={mobileView}
            className={cn(
              // Phone: full width column, hidden when workspace tab active.
              "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
              showMobileToggle &&
                "data-[mobile-view=workspace]:hidden md:flex!",
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
              showBrowser={showBrowser}
              onToggleBrowser={handleToggleBrowser}
              showWorkspace={showWorkspace}
              onToggleWorkspace={handleToggleWorkspace}
              canShowBrowser={browserAvailable}
              canShowWorkspace={workspaceAvailable}
              viewMode={runtime.viewMode}
              onViewModeChange={runtime.setViewMode}
              deepResearchEnabled={deepResearchEnabled}
              researchEnabled={researchEnabled}
              codeAgentsEnabled={codeAgentsEnabled}
              loopsEnabled={loopsEnabled}
              missionsEnabled={missionsEnabled}
              goalsEnabled={goalsEnabled}
              compressEnabled={compressEnabled}
              researchSources={runtime.researchSources}
              hideTurnSummary={hideTurnSummary}
              agentId={agentId}
              onOpenIntegrations={onOpenIntegrations}
            />
          </div>
          {rightStackVisible && (
            <div
              data-testid="right-stack"
              data-mobile-view={mobileView}
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
                  className={cn(
                    "min-h-0 w-full overflow-hidden",
                    bothPanesVisible
                      ? "h-1/2 border-b border-line"
                      : "h-full",
                  )}
                >
                  <BrowserPane
                    sessionId={sessionId}
                    state={browserState}
                    adapter={adapter}
                    onClose={() => setShowBrowser(false)}
                  />
                </div>
              )}
              {workspaceVisible && (
                <div
                  data-testid="workspace-panel-frame"
                  className={
                    bothPanesVisible
                      ? "h-1/2 min-h-0 w-full overflow-hidden"
                      : "min-h-0 h-full"
                  }
                >
                  <WorkspacePanel
                    adapter={adapter}
                    sessionId={sessionId}
                    selectedPath={workspacePath}
                    onSelectedPathChange={setWorkspacePath}
                    refreshSignal={runtime.workspaceRefreshKey}
                    disabled={effectiveDisabled}
                    fillParent={bothPanesVisible}
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
