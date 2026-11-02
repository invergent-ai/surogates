import { useEffect, useMemo, useState } from "react";
import { AgentChat, type AgentChatSession } from "@invergent/agent-chat-react";
import { Button } from "@invergent/ui-sdk/button";
import { EmptyState } from "@invergent/ui-sdk/empty-state";
import { ListRow } from "@invergent/ui-sdk/list-row";
import { SectionLabel } from "@invergent/ui-sdk/section-label";
import { Sidebar, SidebarBrand } from "@invergent/ui-sdk/sidebar";
import { MessageSquareIcon, PlusIcon, TrashIcon } from "lucide-react";
import { createExampleChatAdapter } from "./adapter";

// Titles and statuses change as turns run; the list is polled rather than wired to events.
const REFRESH_MS = 2000;

export function App() {
  const adapter = useMemo(() => createExampleChatAdapter(), []);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<AgentChatSession[]>([]);

  async function refreshSessions() {
    const list = await adapter.listSessions({ limit: 50, offset: 0 });
    setSessions(list.sessions);
    setSessionId((current) => current ?? list.sessions[0]?.id ?? null);
  }

  useEffect(() => {
    void refreshSessions();
    const timer = setInterval(() => void refreshSessions(), REFRESH_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapter]);

  async function newSession() {
    const session = await adapter.createSession({});
    setSessionId(session.id);
    await refreshSessions();
  }

  async function deleteCurrentSession() {
    if (!sessionId || !adapter.deleteSession) return;
    await adapter.deleteSession({ sessionId });
    setSessionId(null);
    await refreshSessions();
  }

  return (
    <main className="flex h-full w-full overflow-hidden bg-background text-foreground">
      <Sidebar>
        <SidebarBrand title="Surogates" subtitle="Agent chat example" />
        <div className="px-3 pb-2">
          <Button className="w-full" variant="outline" onClick={() => void newSession()}>
            <PlusIcon aria-hidden="true" />
            New session
          </Button>
        </div>
        <SectionLabel label="Sessions" className="mx-3" />
        <nav aria-label="Sessions" className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-3 py-1">
          {sessions.map((session) => (
            <ListRow
              key={session.id}
              icon={MessageSquareIcon}
              label={session.title || "New chat"}
              meta={session.status}
              isActive={session.id === sessionId}
              onClick={() => setSessionId(session.id)}
            />
          ))}
          {sessions.length === 0 && (
            <EmptyState className="py-8" logoClassName="size-9" title="No sessions yet" />
          )}
        </nav>
        <div className="px-3 pb-3">
          <Button
            className="w-full"
            variant="ghost"
            disabled={!sessionId}
            onClick={() => void deleteCurrentSession()}
          >
            <TrashIcon aria-hidden="true" />
            Delete current
          </Button>
        </div>
      </Sidebar>
      <section className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <AgentChat
          adapter={adapter}
          sessionId={sessionId}
          onSessionChange={(nextSessionId) => {
            setSessionId(nextSessionId);
            void refreshSessions();
          }}
        />
      </section>
    </main>
  );
}
