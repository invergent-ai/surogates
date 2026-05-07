import { useEffect, useMemo, useState } from "react";
import { AgentChat, type AgentChatSession } from "@invergent/agent-chat-react";
import {
  MessageSquareIcon,
  PlusIcon,
  TrashIcon,
} from "lucide-react";
import { createExampleChatAdapter } from "./adapter";

interface Config {
  model: string;
  baseUrl: string;
  hasApiKey: boolean;
}

export function App() {
  const adapter = useMemo(() => createExampleChatAdapter(), []);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<AgentChatSession[]>([]);
  const [config, setConfig] = useState<Config | null>(null);

  useEffect(() => {
    void refreshSessions();
    fetch("/api/config")
      .then((response) => response.json() as Promise<Config>)
      .then(setConfig)
      .catch(() => setConfig(null));
  }, []);

  async function refreshSessions() {
    const list = await adapter.listSessions({ limit: 20, offset: 0 });
    setSessions(list.sessions);
    if (!sessionId && list.sessions[0]) setSessionId(list.sessions[0].id);
  }

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
    <main className="app-shell">
      <aside className="session-sidebar">
        <div className="sidebar-header">
          <span className="brand-mark">
            <MessageSquareIcon aria-hidden="true" />
          </span>
          <div>
            <h1>Surogates</h1>
            <p>Agent Chat Example</p>
          </div>
        </div>

        <div className="sidebar-actions">
          <button className="new-session-button" type="button" onClick={() => void newSession()}>
            <PlusIcon aria-hidden="true" />
            New session
          </button>
          <div className="config-panel">
            <span>{config?.model ?? "OpenAI-compatible streaming"}</span>
            <strong>{config?.hasApiKey ? "API key configured" : "API key missing"}</strong>
          </div>
        </div>

        <nav className="session-list" aria-label="Sessions">
          {sessions.map((session) => (
            <button
              key={session.id}
              className={`session-row ${session.id === sessionId ? "active" : ""}`}
              type="button"
              onClick={() => setSessionId(session.id)}
            >
              <MessageSquareIcon aria-hidden="true" />
              <span>
                <strong>{session.title || "New session"}</strong>
                <small>{session.model ?? config?.model ?? "default"} · {session.status}</small>
              </span>
            </button>
          ))}
          {sessions.length === 0 && (
            <div className="empty-sessions">No sessions yet</div>
          )}
        </nav>

        <button
          className="delete-session-button"
          type="button"
          disabled={!sessionId}
          onClick={() => void deleteCurrentSession()}
        >
          <TrashIcon aria-hidden="true" />
          Delete current
        </button>
      </aside>
      <section className="chat-surface">
        <AgentChat
          adapter={adapter}
          sessionId={sessionId}
          onSessionChange={(nextSessionId) => {
            setSessionId(nextSessionId);
            void refreshSessions();
          }}
          disabled={config?.hasApiKey === false}
        />
      </section>
    </main>
  );
}
