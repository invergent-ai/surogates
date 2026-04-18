// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { PlusIcon, MessageSquareIcon, LogOutIcon, TrashIcon, SunIcon, MoonIcon, SettingsIcon, BookOpenIcon } from "lucide-react";
import { useTheme } from "next-themes";
import { useAppStore } from "@/stores/app-store";
import { logout } from "@/api/auth";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { formatDistanceToNow } from "date-fns";

export function SessionSidebar() {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const sessions = useAppStore((s) => s.sessions);
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const setActiveSession = useAppStore((s) => s.setActiveSession);
  const createSession = useAppStore((s) => s.createSession);
  const deleteSession = useAppStore((s) => s.deleteSession);
  const user = useAppStore((s) => s.user);
  const { theme, setTheme } = useTheme();

  async function handleNewSession() {
    const session = await createSession({});
    if (session) {
      void navigate({ to: "/chat/$sessionId", params: { sessionId: session.id } });
    }
  }

  function handleSelectSession(sessionId: string) {
    setActiveSession(sessionId);
    void navigate({ to: "/chat/$sessionId", params: { sessionId } });
  }

  function handleLogout() {
    logout();
    void navigate({ to: "/login" });
  }

  return (
    <aside
      className={cn(
        "bg-card border-r border-line flex flex-col overflow-hidden z-10 transition-all duration-200",
        collapsed ? "w-14 min-w-14" : "w-60 min-w-60",
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
              Surogates
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
          {!collapsed && "New session"}
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
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto py-1">
        {sessions.map((session) => {
          const isActive = session.id === activeSessionId;
          const title = session.title ?? "New session";
          const time = formatDistanceToNow(new Date(session.updated_at), {
            addSuffix: true,
          });

          return (
            <div
              role="button"
              tabIndex={0}
              key={session.id}
              className={cn(
                "group flex items-center gap-2 w-full cursor-pointer transition-colors text-left",
                collapsed ? "justify-center py-2" : "px-3 py-2 my-px",
                isActive
                  ? "bg-line text-foreground border-l-2 border-l-primary"
                  : "bg-transparent text-subtle hover:bg-input hover:text-foreground border-l-2 border-l-transparent",
              )}
              onClick={() => handleSelectSession(session.id)}
              onKeyDown={(e) => { if (e.key === "Enter") handleSelectSession(session.id); }}
            >
              {collapsed ? (
                <MessageSquareIcon className="w-4 h-4 shrink-0" />
              ) : (
                <>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm truncate">{title}</div>
                    <div className="text-xs text-faint truncate">
                      {session.model ?? "default"} &middot; {time}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-destructive/10 hover:text-destructive transition-all"
                    onClick={(e) => {
                      e.stopPropagation();
                      void deleteSession(session.id);
                    }}
                  >
                    <TrashIcon className="w-3.5 h-3.5" />
                  </button>
                </>
              )}
            </div>
          );
        })}
        {sessions.length === 0 && !collapsed && (
          <div className="px-4 py-8 text-center text-sm text-faint">
            No sessions yet
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
