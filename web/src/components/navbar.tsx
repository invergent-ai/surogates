// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import {
  MissionsPanel,
  ScheduledWorkPanel,
  SessionTreePanel,
  useInboxUnreadCount,
} from "@invergent/agent-chat-react";
import {
  PlusIcon,
  MessageSquareIcon,
  InboxIcon,
  LogOutIcon,
  SunIcon,
  MoonIcon,
  SettingsIcon,
  BookOpenIcon,
  UsersIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { useAppStore } from "@/stores/app-store";
import { logout } from "@/api/auth";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { surogatesWebChatAdapter } from "@/features/chat";

export function SessionSidebar() {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const fetchSessions = useAppStore((s) => s.fetchSessions);
  const removeSession = useAppStore((s) => s.removeSession);
  // Store-backed list drives the empty-state placeholder and collapsed-mode
  // icon column; SessionTreePanel returns null while loading and when empty.
  const sessions = useAppStore((s) => s.sessions);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);
  const user = useAppStore((s) => s.user);
  const { theme, setTheme } = useTheme();
  const { unreadCount } = useInboxUnreadCount(surogatesWebChatAdapter);

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
    void navigate({
      to: "/missions/$missionId",
      params: { missionId },
    });
  }

  function handleLogout() {
    logout();
    void navigate({ to: "/login" });
  }

  return (
    <aside
      className={cn(
        "bg-card border-r border-line flex flex-col overflow-hidden z-10 transition-all duration-200",
        collapsed ? "w-14 min-w-14" : "w-80 min-w-80",
      )}
    >
      {/* Header */}
      <div
        className={cn(
          "flex items-center border-b border-line min-h-14",
          collapsed ? "justify-center py-4" : "px-4 py-4 gap-2.5",
        )}
      >
        <div className="w-7 h-7 rounded-md bg-primary flex items-center justify-center shrink-0">
          <MessageSquareIcon className="w-4 h-4 text-primary-foreground" />
        </div>
        {!collapsed && (
          <div>
            <div className="font-bold text-foreground tracking-tight">
              Surogate
            </div>
            <div className="text-xs text-muted-foreground tracking-wide uppercase">
              Agent Chat
            </div>
          </div>
        )}
      </div>

      {/* New session button */}
      <div className={cn("border-b border-line", collapsed ? "p-1.5" : "p-3")}>
        <Button
          variant="outline"
          onClick={handleNewSession}
          className={cn(
            "w-full gap-2",
            collapsed ? "justify-center px-0" : "justify-start",
          )}
        >
          <PlusIcon className="w-4 h-4" />
          {!collapsed && "New chat"}
        </Button>
        <Button
          variant="ghost"
          onClick={() => void navigate({ to: "/skills" })}
          className={cn(
            "w-full gap-2 mt-1",
            collapsed ? "justify-center px-0" : "justify-start",
          )}
        >
          <BookOpenIcon className="w-4 h-4" />
          {!collapsed && "Skills"}
        </Button>
        <Button
          variant="ghost"
          onClick={() => void navigate({ to: "/agents" })}
          className={cn(
            "w-full gap-2 mt-1",
            collapsed ? "justify-center px-0" : "justify-start",
          )}
        >
          <UsersIcon className="w-4 h-4" />
          {!collapsed && "Sub-agents"}
        </Button>
        <Button
          variant="ghost"
          onClick={() => void navigate({ to: "/inbox" })}
          className={cn(
            "w-full gap-2 mt-1 relative",
            collapsed ? "justify-center px-0" : "justify-start",
          )}
        >
          <InboxIcon className="w-4 h-4" />
          {!collapsed && "Inbox"}
          {unreadCount > 0 && (
            <span
              className={cn(
                "inline-flex h-5 min-w-5 items-center justify-center bg-primary px-1 text-[0.625rem] font-semibold text-primary-foreground",
                collapsed
                  ? "absolute right-1 top-0 h-4 min-w-4 px-0.5 text-[0.55rem]"
                  : "ml-auto",
              )}
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </Button>
      </div>

      <div className="min-h-0 flex-1 flex flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto py-1">
          {collapsed ? (
            sessions.map((session) => {
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
            })
          ) : (
            <SessionTreePanel
              adapter={surogatesWebChatAdapter}
              loadList
              sessionId={activeSessionId ?? undefined}
              activeSessionId={activeSessionId ?? undefined}
              hideHeader
              onSessionSelect={handleSelectSession}
              onSessionDelete={handleSessionDeleted}
            />
          )}
          {!collapsed && !sessionsLoading && sessions.length === 0 && (
            <div className="px-4 py-8 text-center text-sm text-faint">
              No sessions yet
            </div>
          )}
        </div>
        {!collapsed && (
          <div className="shrink-0 max-h-[45%] overflow-y-auto">
            <MissionsPanel
              adapter={surogatesWebChatAdapter}
              onMissionSelect={handleMissionSelect}
            />
            <ScheduledWorkPanel
              adapter={surogatesWebChatAdapter}
              onSessionSelect={handleSelectSession}
              onScheduleCancel={handleScheduleChanged}
              onScheduleRunNow={handleScheduleChanged}
            />
          </div>
        )}
      </div>

      {/* Footer */}
      <div className={cn("border-t border-line", collapsed ? "py-2" : "p-3")}>
        {!collapsed && (
          <>
            {user && (
              <button
                type="button"
                onClick={() => void navigate({ to: "/settings" })}
                className="flex items-center gap-2 px-1 py-1.5 mb-1.5 w-full rounded-md hover:bg-input transition-colors cursor-pointer"
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
                className="flex items-center gap-2 flex-1 px-2.5 py-1.5 rounded-md text-sm text-subtle hover:bg-input hover:text-foreground transition-colors"
              >
                <LogOutIcon className="w-4 h-4" />
                Sign out
              </button>
              <button
                type="button"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                className="p-1.5 rounded-md text-subtle hover:bg-input hover:text-foreground transition-colors"
                aria-label="Toggle theme"
              >
                {theme === "dark" ? (
                  <SunIcon className="w-4 h-4" />
                ) : (
                  <MoonIcon className="w-4 h-4" />
                )}
              </button>
            </div>
          </>
        )}
        <button
          type="button"
          onClick={() => setCollapsed(!collapsed)}
          className="flex items-center justify-center w-full border-none cursor-pointer py-1.5 bg-transparent text-faint mt-1 hover:text-subtle"
        >
          {collapsed ? "▸" : "◂"}
        </button>
      </div>
    </aside>
  );
}
