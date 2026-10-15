// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import {
  MissionsPanel,
  ScheduledWorkPanel,
  SessionTreePanel,
  sessionComputer,
  useChatViewMode,
  useInboxUnreadCount,
} from "@invergent/agent-chat-react";
import { Link, useNavigate, useRouterState } from "@tanstack/react-router";
import {
  BookOpenIcon,
  InboxIcon,
  LaptopIcon,
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
import { Badge } from "@invergent/ui-sdk/badge";
import { ListRow } from "@invergent/ui-sdk/list-row";
import {
  SidebarAction,
  SidebarBrand,
  SidebarUserBar,
  useSidebar,
} from "@invergent/ui-sdk/sidebar";
import { surogatesWebChatAdapter } from "@/features/chat";
import { getDesktop } from "@/lib/desktop-bridge";
import { cn } from "@/lib/utils";
import { useAppStore } from "@/stores/app-store";
import { slashCommandEnabled } from "@/stores/capabilities-slice";

// Width and the rail are owned by the parent (`AppShell`): in its `Sidebar`
// the rows shrink to their icons when the rail is collapsed; in the phone
// sheet everything draws open.
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
  // "Multi session" off pins each user to one conversation: no "New
  // chat", no session picker.  ``null`` (unknown) fails open.
  const singleSession = useAppStore((s) => s.multiSession) === false;
  const { theme, setTheme } = useTheme();
  const { unreadCount } = useInboxUnreadCount(surogatesWebChatAdapter);
  const viewMode = useChatViewMode();
  const isSimpleMode = viewMode === "simple";
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const { collapsed } = useSidebar();
  const userName = user ? (user.display_name ?? user.email) : "Account";

  function handleNewSession() {
    setActiveSession(null);
    void navigate({ to: "/chat" });
  }

  // Single-session return-to-chat: land on the pinned conversation if one is
  // already selected, otherwise fall through to `/chat`, where the chat page
  // adopts the newest usable session.
  function handleOpenChat() {
    if (activeSessionId) {
      void navigate({
        to: "/chat/$sessionId",
        params: { sessionId: activeSessionId },
      });
    } else {
      void navigate({ to: "/chat" });
    }
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
    // In Surogate Desktop the app logs out, this computer's access with it, and clears this page.
    const desktop = getDesktop();
    if (desktop) {
      void desktop.signOut();
      return;
    }
    logout();
    void navigate({ to: "/login" });
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <SidebarBrand title="Surogate" subtitle="Agent Chat" link={<Link to="/" />} />

      {/* Nav rows. Each names itself (aria-label): in the rail its label
          hides with the rest of it. */}
      <div
        className={cn(
          "flex flex-col gap-0.5 border-b border-line",
          collapsed ? "p-1.5" : "p-2.5",
        )}
      >
        {singleSession ? (
          // One dedicated conversation per user: no "New chat", but the
          // sidebar must still offer a way back into it from other pages.
          <ListRow
            icon={MessageSquareIcon}
            label="Chat"
            aria-label="Chat"
            isActive={pathname.startsWith("/chat")}
            onClick={handleOpenChat}
          />
        ) : (
          <ListRow
            icon={PlusIcon}
            label="New chat"
            aria-label="New chat"
            onClick={handleNewSession}
          />
        )}
        {!isSimpleMode && (
          <ListRow
            asChild
            icon={BookOpenIcon}
            label="Skills"
            aria-label="Skills"
            isActive={pathname.startsWith("/skills")}
          >
            <Link to="/skills" />
          </ListRow>
        )}
        {!isSimpleMode && (
          <ListRow
            asChild
            icon={UsersIcon}
            label="Sub-agents"
            aria-label="Sub-agents"
            isActive={pathname.startsWith("/agents")}
          >
            <Link to="/agents" />
          </ListRow>
        )}
        <div className="relative">
          <ListRow
            asChild
            icon={InboxIcon}
            label="Inbox"
            aria-label={unreadCount > 0 ? `Inbox, ${unreadCount} unread` : "Inbox"}
            isActive={pathname.startsWith("/inbox")}
            badge={unreadCount > 0 && <Badge>{unreadCount > 99 ? "99+" : unreadCount}</Badge>}
          >
            <Link to="/inbox" />
          </ListRow>
          {/* The rail has no room for the count beside the label: it sits
              on the icon's corner, solid so the icon under it cannot show
              through. */}
          {collapsed && unreadCount > 0 && (
            <Badge
              aria-hidden="true"
              className="pointer-events-none absolute top-0 right-0.5 h-4 min-w-4 bg-primary px-1 py-0 text-[10px] font-semibold text-primary-foreground"
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </Badge>
          )}
        </div>
      </div>

      {/* Sessions list — hidden entirely in single-session mode: the one
          conversation is always active, so a picker only invites hopping
          back into pre-restriction sessions. */}
      <div className="min-h-0 flex-1 flex flex-col">
        {!singleSession && (
          <div className="min-h-0 flex-1 overflow-y-auto py-1">
            {/* The rail: an icon per session. Both lists stay mounted, hidden
                in turn, so hovering the rail open does not reload the tree. */}
            <div className={cn("space-y-0.5 px-1.5", !collapsed && "hidden")}>
              {sessions.map((session) => {
                // A chat on a folder of the user's computer shows a laptop, and says which.
                const computer = sessionComputer(session.config);
                const title = session.title ?? "New session";
                return (
                  <ListRow
                    key={session.id}
                    icon={computer ? LaptopIcon : MessageSquareIcon}
                    label={title}
                    aria-label={computer ? `${title}, on ${computer}` : title}
                    isActive={session.id === activeSessionId}
                    onClick={() => handleSelectSession(session.id)}
                          />
                );
              })}
            </div>

            <div className={cn(collapsed && "hidden")}>
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
        )}

        {/* Missions + Scheduled work — expanded only */}
        <div
          className={cn(
            "shrink-0 max-h-[45%] overflow-y-auto",
            collapsed && "hidden",
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

      <SidebarUserBar
        avatar={
          <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary/20 text-xs font-bold text-primary">
            {userName[0]?.toUpperCase() ?? "?"}
          </span>
        }
        name={userName}
      >
        <SidebarAction
          label={theme === "dark" ? "Light mode" : "Dark mode"}
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? <SunIcon /> : <MoonIcon />}
        </SidebarAction>
        <SidebarAction asChild label="Settings">
          <Link to="/settings">
            <SettingsIcon />
          </Link>
        </SidebarAction>
        <SidebarAction label="Sign out" onClick={handleLogout}>
          <LogOutIcon className="size-4" />
        </SidebarAction>
      </SidebarUserBar>
    </div>
  );
}
