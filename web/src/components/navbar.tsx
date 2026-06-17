// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import {
  MissionsPanel,
  ScheduledWorkPanel,
  SessionTreePanel,
  useChatViewMode,
  useInboxUnreadCount,
} from "@invergent/agent-chat-react";
import { useNavigate } from "@tanstack/react-router";
import {
  BookOpenIcon,
  InboxIcon,
  LogOutIcon,
  MessageSquareIcon,
  MoonIcon,
  PlusIcon,
  SettingsIcon,
  SunIcon,
  UsersIcon,
} from "lucide-react";
import { useTheme } from "next-themes";

import { logout } from "@/api/auth";
import { Button } from "@/components/ui/button";
import { surogatesWebChatAdapter } from "@/features/chat";
import { cn } from "@/lib/utils";
import { useAppStore } from "@/stores/app-store";
import { slashCommandEnabled } from "@/stores/capabilities-slice";

// Width and density are owned by the parent (`AppShell`):
//   - desktop aside : `data-mode="aside"` + breakpoints `md` (compact, w-14)
//     and `lg` (expanded, w-80).
//   - phone sheet   : `data-mode="sheet"` always renders expanded.
// We use Tailwind v4's `group-data-[mode=sheet]:*` variants together with
// `lg:*` to flip between compact (icons only) and expanded (full content).
const showExpanded =
  "hidden lg:flex group-data-[mode=sheet]:flex";
const showExpandedInline =
  "hidden lg:inline group-data-[mode=sheet]:inline";
const showExpandedBlock =
  "hidden lg:block group-data-[mode=sheet]:block";
const hideOnExpanded =
  "block lg:hidden group-data-[mode=sheet]:hidden";

export function SessionSidebar() {
  const navigate = useNavigate();
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const fetchSessions = useAppStore((s) => s.fetchSessions);
  const removeSession = useAppStore((s) => s.removeSession);
  const sessions = useAppStore((s) => s.sessions);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);
  const user = useAppStore((s) => s.user);
  const slashCommands = useAppStore((s) => s.slashCommands);
  const { theme, setTheme } = useTheme();
  const { unreadCount } = useInboxUnreadCount(surogatesWebChatAdapter);
  const viewMode = useChatViewMode();
  const isSimpleMode = viewMode === "simple";

  function handleNewSession() {
    setActiveSession(null);
    void navigate({ to: "/chat" });
  }

  function handleSelectSession(sessionId: string) {
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }

  function handleSessionDeleted(sessionId: string) {
    if (sessionId === activeSessionId) {
      void navigate({ to: "/chat" });
    }
    removeSession(sessionId);
  }

  function handleScheduleChanged() {
    void fetchSessions();
  }

  function handleMissionSelect(missionId: string) {
    void navigate({ to: "/missions/$missionId", params: { missionId } });
  }

  function handleLogout() {
    logout();
    void navigate({ to: "/login" });
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {/* Header */}
      <div
        className={cn(
          "flex items-center border-b border-line min-h-14",
          "justify-center py-4",
          "lg:justify-start lg:px-4 lg:py-4 lg:gap-2.5",
          "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-4 group-data-[mode=sheet]:py-4 group-data-[mode=sheet]:gap-2.5",
        )}
      >
        <div className="w-7 h-7 rounded-md bg-primary flex items-center justify-center shrink-0">
          <MessageSquareIcon className="w-4 h-4 text-primary-foreground" />
        </div>
        <div className={showExpandedBlock}>
          <div className="font-bold text-foreground tracking-tight">
            Surogate
          </div>
          <div className="text-xs text-muted-foreground tracking-wide uppercase">
            Agent Chat
          </div>
        </div>
      </div>

      {/* Nav buttons */}
      <div
        className={cn(
          "border-b border-line",
          "p-1.5 lg:p-3 group-data-[mode=sheet]:p-3",
        )}
      >
        <Button
          variant="outline"
          onClick={handleNewSession}
          className={cn(
            "w-full gap-2 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11",
            "justify-center px-0 lg:justify-start lg:px-3",
            "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
          )}
        >
          <PlusIcon className="w-4 h-4" />
          <span className={showExpandedInline}>New chat</span>
        </Button>
        {!isSimpleMode && (
          <Button
            variant="ghost"
            onClick={() => void navigate({ to: "/skills" })}
            className={cn(
              "w-full gap-2 mt-1 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11",
              "justify-center px-0 lg:justify-start lg:px-3",
              "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
            )}
          >
            <BookOpenIcon className="w-4 h-4" />
            <span className={showExpandedInline}>Skills</span>
          </Button>
        )}
        {!isSimpleMode && (
          <Button
            variant="ghost"
            onClick={() => void navigate({ to: "/agents" })}
            className={cn(
              "w-full gap-2 mt-1 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11",
              "justify-center px-0 lg:justify-start lg:px-3",
              "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
            )}
          >
            <UsersIcon className="w-4 h-4" />
            <span className={showExpandedInline}>Sub-agents</span>
          </Button>
        )}
        <Button
          variant="ghost"
          onClick={() => void navigate({ to: "/inbox" })}
          className={cn(
            "w-full gap-2 mt-1 min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11 relative",
            "justify-center px-0 lg:justify-start lg:px-3",
            "group-data-[mode=sheet]:justify-start group-data-[mode=sheet]:px-3",
          )}
        >
          <InboxIcon className="w-4 h-4" />
          <span className={showExpandedInline}>Inbox</span>
          {unreadCount > 0 && (
            <span
              className={cn(
                "inline-flex items-center justify-center bg-primary px-1 text-[0.625rem] font-semibold text-primary-foreground",
                "absolute right-1 top-0 h-4 min-w-4 px-0.5 text-[0.55rem]",
                "lg:static lg:ml-auto lg:h-5 lg:min-w-5 lg:px-1 lg:text-[0.625rem]",
                "group-data-[mode=sheet]:static group-data-[mode=sheet]:ml-auto group-data-[mode=sheet]:h-5 group-data-[mode=sheet]:min-w-5 group-data-[mode=sheet]:px-1 group-data-[mode=sheet]:text-[0.625rem]",
              )}
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </Button>
      </div>

      {/* Sessions list */}
      <div className="min-h-0 flex-1 flex flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto py-1">
          {/* Compact (md only, not sheet): icon-only list */}
          <div className={hideOnExpanded}>
            {sessions.map((session) => {
              const isActive = session.id === activeSessionId;
              return (
                <button
                  key={session.id}
                  type="button"
                  onClick={() => handleSelectSession(session.id)}
                  aria-label={session.title ?? "New session"}
                  className={cn(
                    "flex items-center justify-center w-full py-2 transition-colors border-l-2",
                    isActive
                      ? "bg-line text-foreground border-l-primary"
                      : "bg-transparent text-subtle hover:bg-input hover:text-foreground border-l-transparent",
                  )}
                >
                  <MessageSquareIcon className="w-4 h-4 shrink-0" />
                </button>
              );
            })}
          </div>

          {/* Expanded (lg+ or sheet): SessionTreePanel */}
          <div className={showExpandedBlock}>
            <SessionTreePanel
              adapter={surogatesWebChatAdapter}
              loadList
              sessionId={activeSessionId ?? undefined}
              activeSessionId={activeSessionId ?? undefined}
              hideHeader
              onSessionSelect={handleSelectSession}
              onSessionDelete={handleSessionDeleted}
            />
            {!sessionsLoading && sessions.length === 0 && (
              <div className="px-4 py-8 text-center text-sm text-faint">
                No sessions yet
              </div>
            )}
          </div>
        </div>

        {/* Missions + Scheduled work — expanded only */}
        <div
          className={cn(
            "shrink-0 max-h-[45%] overflow-y-auto",
            showExpandedBlock,
          )}
        >
          {slashCommandEnabled(slashCommands, "mission") && (
            <MissionsPanel
              adapter={surogatesWebChatAdapter}
              onMissionSelect={handleMissionSelect}
            />
          )}
          {slashCommandEnabled(slashCommands, "loop") && (
            <ScheduledWorkPanel
              adapter={surogatesWebChatAdapter}
              onSessionSelect={handleSelectSession}
              onScheduleCancel={handleScheduleChanged}
              onScheduleRunNow={handleScheduleChanged}
            />
          )}
        </div>
      </div>

      {/* Footer — expanded only (icon-mode hides it entirely) */}
      <div
        className={cn(
          "border-t border-line flex-col gap-1",
          "p-3",
          showExpanded,
        )}
      >
        {user && (
          <button
            type="button"
            onClick={() => void navigate({ to: "/settings" })}
            className="flex items-center gap-2 px-1 py-1.5 mb-1.5 w-full rounded-md hover:bg-input transition-colors cursor-pointer min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11"
          >
            <div className="w-6 h-6 rounded-full bg-primary/20 flex items-center justify-center font-bold text-primary text-xs shrink-0">
              {(user.display_name ?? user.email)?.[0]?.toUpperCase() ?? "?"}
            </div>
            <div className="flex-1 min-w-0 text-left">
              <div className="text-subtle font-medium text-sm truncate">
                {user.display_name ?? user.email}
              </div>
            </div>
            <SettingsIcon className="w-3.5 h-3.5 text-faint shrink-0" />
          </button>
        )}
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={handleLogout}
            className="flex items-center gap-2 flex-1 px-2.5 py-1.5 rounded-md text-sm text-subtle hover:bg-input hover:text-foreground transition-colors min-h-11 lg:min-h-9 group-data-[mode=sheet]:min-h-11"
          >
            <LogOutIcon className="w-4 h-4" />
            Sign out
          </button>
          <button
            type="button"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            className="p-1.5 rounded-md text-subtle hover:bg-input hover:text-foreground transition-colors min-h-11 lg:min-h-9 min-w-11 lg:min-w-0 group-data-[mode=sheet]:min-h-11 group-data-[mode=sheet]:min-w-11"
            aria-label="Toggle theme"
          >
            {theme === "dark" ? (
              <SunIcon className="w-4 h-4" />
            ) : (
              <MoonIcon className="w-4 h-4" />
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
