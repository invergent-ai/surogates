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
import { SurogatesAuthError, SurogatesError, SurogatesRateLimitError } from '../errors.js';
import type { QuestionAnswer } from '../protocol.js';
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

interface Question {
  prompt: string;
  choices: { label: string; description?: string }[];
  allowOther: boolean;
}

/** An ``ask_user_question`` call, with the answers once it has them. */
interface QuestionItem {
  id: string;
  role: 'question';
  toolCallId: string;
  intro: string[];
  questions: Question[];
  /** Absent while it waits; empty when it closed unanswered. */
  answers?: QuestionAnswer[];
}

type ChatItem = ChatMessage | QuestionItem;

type RawMessage = {
  id?: string;
  role?: string;
  content?: unknown;
  toolCallId?: string;
  toolCalls?: { id: string; function?: { name?: string; arguments?: string } }[];
};

function parseJson(text: unknown): Record<string, unknown> | undefined {
  try {
    const value = JSON.parse(String(text));
    return value && typeof value === 'object' ? value : undefined;
  } catch {
    return undefined;
  }
}

function toQuestions(raw: unknown): Question[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((q): q is { prompt: string; choices?: unknown; allow_other?: unknown } =>
      !!q && typeof q.prompt === 'string' && q.prompt.trim() !== '')
    .map((q) => {
      const choices = (Array.isArray(q.choices) ? q.choices : []).filter(
        (c): c is { label: string; description?: string } => !!c && typeof c.label === 'string',
      );
      // An open question has nothing to pick, so typing is its only answer.
      return { prompt: q.prompt.trim(), choices, allowOther: q.allow_other !== false || choices.length === 0 };
    });
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

/**
 * Project ``agent.messages`` down to what the visitor sees: user/assistant
 * turns, assistant text with its ``<next_action>`` footer stripped, and the
 * agent's ``ask_user_question`` calls.  An assistant message with no text
 * left (one that only carries tool calls, or only the footer) renders no
 * bubble, so it neither shows empty nor counts as the reply that hides the
 * typing indicator.
 */
function snapshot(messages: ReadonlyArray<RawMessage>): ChatItem[] {
  const results = new Map<string, string>();
  for (const m of messages) {
    if (m.role === 'tool' && m.toolCallId) results.set(m.toolCallId, String(m.content ?? ''));
  }
  const out: ChatItem[] = [];
  for (const m of messages) {
    if (m.role !== 'user' && m.role !== 'assistant') continue;
    let content = typeof m.content === 'string' ? m.content : '';
    if (m.role === 'assistant') content = stripNextAction(content);
    if (content.trim() !== '' || m.role === 'user') {
      out.push({ id: m.id ?? uid(), role: m.role, content });
    }
    for (const call of m.toolCalls ?? []) {
      if (call.function?.name !== 'ask_user_question') continue;
      const args = parseJson(call.function.arguments) ?? {};
      const questions = toQuestions(args.questions);
      if (questions.length === 0) continue;
      const intro = [args.message, args.context].filter(
        (t): t is string => typeof t === 'string' && t.trim() !== '',
      );
      const result = results.get(call.id);
      const responses = result === undefined ? undefined : parseJson(result)?.responses;
      out.push({
        id: `q-${call.id}`,
        role: 'question',
        toolCallId: call.id,
        intro,
        questions,
        ...(result !== undefined && { answers: Array.isArray(responses) ? responses : [] }),
      });
    }
  }
  return out;
}

/** Only render CTA links with a web protocol — the buy URL rides in
 * from server config, but an SDK running on third-party sites must
 * not let any upstream compromise turn it into a `javascript:` URI. */
function safeBuyUrl(url: string | undefined): string | undefined {
  return url && /^https?:\/\//i.test(url) ? url : undefined;
}

/** Visitor-facing copy per paywall sentinel from the server. */
function paywallCopy(code: string): string {
  if (code === 'sign_in_required') {
    return 'This assistant requires an account. Get access, sign in on this site, then send your message again.';
  }
  if (code === 'subscription_required') {
    return 'This assistant requires an active subscription.';
  }
  if (code === 'free_plan_available') {
    return 'This assistant needs a plan, and there is a free one.';
  }
  if (code === 'upgrade_required') {
    return 'You have used this month’s free allowance. Upgrade to keep chatting.';
  }
  if (code === 'insufficient_tokens') {
    return 'You’re out of tokens for this assistant.';
  }
  return 'This assistant requires paid access.';
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

interface QuestionCardProps {
  item: QuestionItem;
  /** What this visitor sent, shown until the agent's result lands. */
  sent?: QuestionAnswer[];
  onAnswer: (answers: QuestionAnswer[]) => Promise<void>;
}

function QuestionCard({ item, sent, onAnswer }: QuestionCardProps) {
  const [picks, setPicks] = useState<(QuestionAnswer | undefined)[]>([]);
  const [sending, setSending] = useState(false);
  const answers = item.answers ?? sent;
  const closed = answers !== undefined || sending;
  const shown = (i: number) =>
    answers ? answers.find((a) => a.question === item.questions[i]!.prompt) : picks[i];

  const submit = async (all: (QuestionAnswer | undefined)[]) => {
    if (closed || all.length !== item.questions.length || all.some((a) => !a?.answer.trim())) return;
    setSending(true);
    try {
      await onAnswer(all.map((a) => ({ ...a!, answer: a!.answer.trim() })));
    } finally {
      setSending(false);
    }
  };

  const pick = (i: number, answer: QuestionAnswer, now: boolean) => {
    const next = item.questions.map((_, j) => (j === i ? answer : picks[j]));
    setPicks(next);
    if (now) void submit(next);
  };

  const single = item.questions.length === 1;
  const needsSend = !single || item.questions.some((q) => q.allowOther);

  return (
    <div class="surg-bubble surg-assistant surg-question">
      {item.intro.map((text, i) => (
        <div key={i} class="surg-question-intro" dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />
      ))}
      {item.questions.map((q, i) => {
        const current = shown(i);
        return (
          <div key={i} class="surg-question-item">
            <div class="surg-question-prompt">{q.prompt}</div>
            {q.choices.map((c) => (
              <button
                key={c.label}
                type="button"
                class={`surg-choice${current?.answer === c.label && !current.is_other ? ' surg-chosen' : ''}`}
                disabled={closed}
                aria-pressed={current?.answer === c.label}
                onClick={() => pick(i, { question: q.prompt, answer: c.label, is_other: false }, single)}
              >
                {c.label}
                {c.description && <span class="surg-choice-note">{c.description}</span>}
              </button>
            ))}
            {q.allowOther && (
              <input
                class="surg-other"
                type="text"
                placeholder={q.choices.length ? 'Something else…' : 'Your answer…'}
                disabled={closed}
                value={current?.is_other || !q.choices.length ? current?.answer ?? '' : ''}
                onInput={(e) =>
                  pick(i, { question: q.prompt, answer: (e.target as HTMLInputElement).value, is_other: q.choices.length > 0 }, false)
                }
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void submit(picks);
                }}
              />
            )}
          </div>
        );
      })}
      {answers?.length === 0 && (
        <div class="surg-question-note">This question was already answered or has expired.</div>
      )}
      {needsSend && !answers && (
        <button
          type="button"
          class="surg-question-send"
          disabled={closed || item.questions.some((_, i) => !picks[i]?.answer.trim())}
          onClick={() => void submit(picks)}
        >
          Send
        </button>
      )}
    </div>
  );
}

export function Widget({ agent, config, registerOpenControl, onOpenChange }: WidgetProps) {
  const inline = !!config.inline;
  const [open, setOpen] = useState(inline || !!config.openByDefault);
  const [messages, setMessages] = useState<ChatItem[]>(() => snapshot(agent.messages));
  const [sent, setSent] = useState<Record<string, QuestionAnswer[]>>({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [paywall, setPaywall] = useState<{ code: string; buyUrl?: string } | null>(null);
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
        setPaywall(null);
      },
      onRunFinishedEvent: () => setRunning(false),
      onRunErrorEvent: ({ event }) => {
        setRunning(false);
        const code = (event as { code?: string }).code;
        if (code === 'paywall') {
          setPaywall(agent.lastPaywall ?? { code: 'payment_required' });
          return;
        }
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

  const answer = (toolCallId: string) => async (answers: QuestionAnswer[]) => {
    try {
      await agent.answerQuestion(toolCallId, answers);
      setSent((prev) => ({ ...prev, [toolCallId]: answers }));
      setError(null);
    } catch (err) {
      const status = err instanceof SurogatesError ? err.status : undefined;
      if (status === 409) {
        setSent((prev) => ({ ...prev, [toolCallId]: [] }));
      } else if (err instanceof SurogatesAuthError) {
        setError('Couldn’t send your answer. Please reload the page.');
      } else {
        setError(friendlyError(err));
      }
    }
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
  const hasAssistant = messages.some((m) => m.role !== 'user');
  const showWelcome = !!welcomeText && !hasAssistant;
  // A question waiting on the visitor is the agent's turn paused, not working.
  // Only the latest question can still be waiting; an older one left open
  // was dropped by its turn.
  const lastQuestion = [...messages].reverse().find((m): m is QuestionItem => m.role === 'question');
  const awaitingVisitor = !!lastQuestion && !lastQuestion.answers && !sent[lastQuestion.toolCallId];
  const showTyping = running && !awaitingVisitor && messages[messages.length - 1]?.role !== 'assistant';

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
          m.role === 'question' ? (
            <QuestionCard key={m.id} item={m} sent={sent[m.toolCallId]} onAnswer={answer(m.toolCallId)} />
          ) : m.role === 'assistant' ? (
            <div
              key={m.id}
              class="surg-bubble surg-assistant"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(m.content) }}
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

      {paywall && (
        <div class="surg-paywall">
          <span>{paywallCopy(paywall.code)}</span>
          {safeBuyUrl(paywall.buyUrl) && (
            <a
              class="surg-paywall-cta"
              href={safeBuyUrl(paywall.buyUrl)}
              target="_blank"
              rel="noopener noreferrer"
            >
              Get access
            </a>
          )}
        </div>
      )}

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
