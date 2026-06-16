/**
 * The Preact chat UI rendered inside the widget's Shadow DOM.
 *
 * It is a thin presentation layer over the headless :class:`WebsiteAgent`:
 * all transport, auth, and streaming live in the agent; this component
 * only subscribes to the AG-UI event stream, mirrors ``agent.messages``
 * into local state, and drives ``runAgent`` from the composer.
 */
import { useEffect, useRef, useState } from 'preact/hooks';

import type { WebsiteAgent } from '../agent.js';
import { SurogatesAuthError, SurogatesRateLimitError } from '../errors.js';
import { renderMarkdown } from './markdown.js';
import { stripNextAction } from './next-action.js';
import type { MountConfig } from './types.js';

// Shown when the operator hasn't set their own. Kept friendly and
// provider-neutral so an unconfigured widget still looks finished.
const DEFAULT_SUBTITLE = 'Typically replies instantly';
const DEFAULT_WELCOME = 'Hi! 👋 How can I help you today?';

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
}

interface WidgetProps {
  agent: WebsiteAgent;
  config: MountConfig;
  /** Lets the imperative handle in ``mount`` drive the open/closed state. */
  registerOpenControl: (setOpen: (open: boolean) => void) => void;
  /** Reports open-state changes back so ``mount``'s ``toggle()`` stays in sync. */
  onOpenChange?: (open: boolean) => void;
}

function uid(): string {
  return `u-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

/** Project ``agent.messages`` down to the renderable user/assistant turns. */
function snapshot(messages: ReadonlyArray<{ id?: string; role?: string; content?: unknown }>): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const m of messages) {
    if (m.role !== 'user' && m.role !== 'assistant') continue;
    const content = typeof m.content === 'string' ? m.content : '';
    out.push({ id: m.id ?? uid(), role: m.role, content });
  }
  return out;
}

/** Map an SDK error (thrown or via RUN_ERROR) to visitor-facing copy. */
function friendlyError(err: { code?: string; message?: string } | unknown): string {
  if (err instanceof SurogatesAuthError) {
    return 'This chat isn’t accepting messages from this site yet. The site owner needs to allow this origin.';
  }
  if (err instanceof SurogatesRateLimitError) {
    return 'You’re sending messages too quickly. Please wait a moment and try again.';
  }
  if (err && typeof err === 'object') {
    const code = (err as { code?: string }).code;
    if (code === 'auth') {
      return 'This chat isn’t accepting messages from this site yet. The site owner needs to allow this origin.';
    }
    if (code === 'sse_closed') return 'Connection lost. Please send your message again.';
    const message = (err as { message?: string }).message;
    if (message && message.length < 160) return message;
  }
  return 'Something went wrong. Please try again.';
}

const ICON_CHAT = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
  </svg>
);
const ICON_CLOSE = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);
const ICON_SEND = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" />
  </svg>
);

export function Widget({ agent, config, registerOpenControl, onOpenChange }: WidgetProps) {
  const inline = !!config.inline;
  const [open, setOpen] = useState(inline || !!config.openByDefault);
  const [messages, setMessages] = useState<ChatMessage[]>(() => snapshot(agent.messages));
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState('');
  // Only the async input is state; the title is derived at render so a
  // configured title (incl. live handle.update()) always wins, falling back
  // to the bootstrapped agent name, then a generic label.
  const [agentName, setAgentName] = useState('');
  const title = config.title || agentName || 'Assistant';

  const listRef = useRef<HTMLDivElement>(null);
  const bootstrapStarted = useRef(false);

  // Expose the open setter to the mount() imperative handle.
  useEffect(() => registerOpenControl(setOpen), [registerOpenControl]);
  useEffect(() => onOpenChange?.(open), [open, onOpenChange]);

  // Mirror the agent's event stream into local UI state.
  useEffect(() => {
    const sub = agent.subscribe({
      onMessagesChanged: () => setMessages(snapshot(agent.messages)),
      onRunStartedEvent: () => {
        setRunning(true);
        setError(null);
      },
      onRunFinishedEvent: () => setRunning(false),
      onRunErrorEvent: ({ event }) => {
        setRunning(false);
        setError(friendlyError(event));
      },
    });
    return () => sub.unsubscribe();
  }, [agent]);

  // Validate config + resolve the header title the first time the panel
  // is opened.  Eager bootstrap on open (not page-load) means visitors
  // who never open the chat don't create server sessions; opening is a
  // clear intent signal and surfaces misconfiguration immediately.
  useEffect(() => {
    if (!open || bootstrapStarted.current) return;
    bootstrapStarted.current = true;
    agent
      .ensureBootstrapped()
      .then((res) => {
        if (res.agentName) setAgentName(res.agentName);
      })
      .catch((err) => {
        // Allow a retry the next time the panel opens (transient network, etc.).
        bootstrapStarted.current = false;
        setError(friendlyError(err));
      });
  }, [open, agent]);

  // Keep the transcript pinned to the latest message.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, running]);

  const send = () => {
    const content = input.trim();
    if (!content || running) return;
    setInput('');
    agent.addMessage({ id: uid(), role: 'user', content } as never);
    setMessages(snapshot(agent.messages));
    void agent.runAgent().catch(() => {
      /* Failures surface through onRunErrorEvent; swallow the rejection. */
    });
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  // An unconfigured widget always shows sensible default copy: a missing
  // subtitle/welcome falls back to the defaults. Note Studio/the embed omit
  // empty fields, so *clearing* the field in Studio is indistinguishable from
  // never setting it — both show the default. There is intentionally no
  // "show nothing" state today; if suppression is ever needed, add an explicit
  // sentinel rather than relying on empty string.
  const subtitle = config.subtitle ?? DEFAULT_SUBTITLE;
  const welcomeText = config.welcomeMessage ?? DEFAULT_WELCOME;
  const hasAssistant = messages.some((m) => m.role === 'assistant');
  const showWelcome = !!welcomeText && !hasAssistant;
  const showTyping = running && messages[messages.length - 1]?.role !== 'assistant';

  if (!inline && !open) {
    return (
      <button
        type="button"
        class={`surg-launcher surg-pos-${config.position ?? 'bottom-right'}`}
        aria-label="Open chat"
        onClick={() => setOpen(true)}
      >
        {config.logoUrl ? (
          <img class="surg-launcher-logo" src={config.logoUrl} alt="" />
        ) : (
          ICON_CHAT
        )}
      </button>
    );
  }

  const panelClass = inline
    ? 'surg-panel surg-inline'
    : `surg-panel surg-pos-${config.position ?? 'bottom-right'}`;

  return (
    <div class={panelClass} role="dialog" aria-label={title}>
      <div class="surg-header">
        <div class="surg-header-id">
          {config.logoUrl && <img class="surg-logo" src={config.logoUrl} alt="" />}
          <div class="surg-header-text">
            <span class="surg-title">{title}</span>
            {subtitle && <span class="surg-subtitle">{subtitle}</span>}
          </div>
        </div>
        {!inline && (
          <button type="button" class="surg-close" aria-label="Close chat" onClick={() => setOpen(false)}>
            {ICON_CLOSE}
          </button>
        )}
      </div>

      <div class="surg-messages" ref={listRef}>
        {showWelcome && (
          <div
            class="surg-bubble surg-assistant"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: renderMarkdown(welcomeText) }}
          />
        )}
        {messages.map((m) =>
          m.role === 'assistant' ? (
            <div
              key={m.id}
              class="surg-bubble surg-assistant"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(stripNextAction(m.content)) }}
            />
          ) : (
            <div key={m.id} class="surg-bubble surg-user">
              {m.content}
            </div>
          ),
        )}
        {showTyping && (
          <div class="surg-bubble surg-assistant surg-typing">
            <span /><span /><span />
          </div>
        )}
      </div>

      {error && <div class="surg-error">{error}</div>}

      <div class="surg-composer">
        <textarea
          class="surg-input"
          rows={1}
          placeholder="Type a message…"
          value={input}
          disabled={running}
          onInput={(e) => setInput((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
        />
        <button type="button" class="surg-send" aria-label="Send" disabled={running || !input.trim()} onClick={send}>
          {ICON_SEND}
        </button>
      </div>
      <div class="surg-powered">
        Powered by{' '}
        <a href="https://surogate.ai/" target="_blank" rel="noopener noreferrer">
          Surogate
        </a>
      </div>
    </div>
  );
}
