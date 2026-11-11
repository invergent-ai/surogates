/**
 * UI smoke tests for :func:`mount` — the self-mounting widget.
 *
 * These exercise the real :class:`WebsiteAgent` (with ``fetch`` +
 * ``EventSource`` stubbed exactly as in ``agent.test.ts``) rendered
 * through the Preact UI in a happy-dom Shadow DOM.  They lock down the
 * three things a "drop a script tag" embed has to get right: the host
 * element + shadow root materialise, the panel opens, and a sent
 * message round-trips into a streamed assistant bubble.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SURG_EVENT } from '../src/constants.js';
import { mount } from '../src/ui/mount.js';

// --- Test doubles (mirrors agent.test.ts) ----------------------------------

class FakeEventSource {
  static lastInstance: FakeEventSource | undefined;
  readonly url: string;
  readyState = 1;
  onerror: ((ev: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<(ev: MessageEvent) => void>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.lastInstance = this;
  }
  addEventListener(type: string, fn: (ev: MessageEvent) => void): void {
    let set = this.listeners.get(type);
    if (!set) this.listeners.set(type, (set = new Set()));
    set.add(fn);
  }
  removeEventListener(type: string, fn: (ev: MessageEvent) => void): void {
    this.listeners.get(type)?.delete(fn);
  }
  close(): void {
    this.readyState = 2;
  }
  emit(eventName: string, data: unknown, id: number): void {
    if (this.readyState === 2) return;
    const set = this.listeners.get(eventName);
    if (!set) return;
    const ev = new MessageEvent(eventName, { data: JSON.stringify(data), lastEventId: String(id) });
    for (const fn of Array.from(set)) fn(ev);
  }
  static reset(): void {
    FakeEventSource.lastInstance = undefined;
  }
}

function makeResponse(status: number, body: unknown): Response {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), { status });
}

/** Bootstrap-then-send fetch stub: 201 on bootstrap, 202 on messages. */
function happyFetch() {
  return vi.fn(async (url: unknown, init: RequestInit | undefined) => {
    if (init?.method === 'POST' && /\/v1\/website\/sessions(\?|$)/.test(String(url))) {
      return makeResponse(201, {
        session_id: 'sess-1',
        csrf_token: 'csrf-1',
        expires_at: Date.now() / 1000 + 3600,
        agent_name: 'Acme Support',
      });
    }
    return makeResponse(202, { event_id: 1 });
  }) as unknown as typeof fetch;
}

async function waitFor(predicate: () => boolean, attempts = 200): Promise<void> {
  for (let i = 0; i < attempts; i++) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error('waitFor predicate never became true');
}

const originalFetch = globalThis.fetch;
const originalEventSource = globalThis.EventSource;

beforeEach(() => {
  FakeEventSource.reset();
  document.body.innerHTML = '';
  (globalThis as unknown as { EventSource: typeof FakeEventSource }).EventSource = FakeEventSource;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalEventSource;
});

function shadowOf(): ShadowRoot {
  const host = document.querySelector('[data-surogates-widget]');
  if (!host || !(host as HTMLElement & { shadowRoot: ShadowRoot | null }).shadowRoot) {
    throw new Error('widget host / shadow root not found');
  }
  return (host as HTMLElement).shadowRoot as ShadowRoot;
}

// --- Tests ------------------------------------------------------------------

describe('mount() — config + lifecycle', () => {
  it('requires apiUrl and publishableKey', () => {
    expect(() => mount({ apiUrl: '', publishableKey: 'surg_wk_x' })).toThrow(/required/);
    expect(() => mount({ apiUrl: 'https://a.test', publishableKey: '' })).toThrow(/required/);
  });

  it('renders a launcher (closed) by default, opens on click', async () => {
    globalThis.fetch = happyFetch();
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x' });

    const sr = shadowOf();
    expect(sr.querySelector('.surg-launcher')).toBeTruthy();
    expect(sr.querySelector('.surg-panel')).toBeNull();

    (sr.querySelector('.surg-launcher') as HTMLButtonElement).click();
    await waitFor(() => !!sr.querySelector('.surg-panel'));
    expect(sr.querySelector('.surg-panel')).toBeTruthy();
    handle.destroy();
  });

  it('inline mode renders the panel immediately with no launcher', () => {
    globalThis.fetch = happyFetch();
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x', inline: true });
    const sr = shadowOf();
    expect(sr.querySelector('.surg-launcher')).toBeNull();
    expect(sr.querySelector('.surg-panel')).toBeTruthy();
    handle.destroy();
  });

  it('applies accent colour and renders the welcome message as markdown', () => {
    globalThis.fetch = happyFetch();
    const handle = mount({
      apiUrl: 'https://a.test',
      publishableKey: 'surg_wk_x',
      inline: true,
      accentColor: '#0ea5e9',
      welcomeMessage: 'Hi! **Ask me** anything.',
    });
    const sr = shadowOf();
    const root = sr.querySelector('.surg-root') as HTMLElement;
    expect(root.style.getPropertyValue('--surg-accent')).toBe('#0ea5e9');
    expect(sr.querySelector('.surg-assistant strong')?.textContent).toBe('Ask me');
    expect(sr.querySelector('.surg-powered')?.textContent).toBe('Powered by Surogate');
    handle.destroy();
  });

  it('renders a logo + subtitle when provided', () => {
    globalThis.fetch = happyFetch();
    const handle = mount({
      apiUrl: 'https://a.test',
      publishableKey: 'surg_wk_x',
      inline: true,
      title: 'Acme',
      subtitle: 'Replies in a minute',
      logoUrl: 'data:image/png;base64,iVBORw0KGgo=',
    });
    const sr = shadowOf();
    const logo = sr.querySelector('.surg-logo') as HTMLImageElement | null;
    expect(logo?.getAttribute('src')).toBe('data:image/png;base64,iVBORw0KGgo=');
    expect(sr.querySelector('.surg-subtitle')?.textContent).toBe('Replies in a minute');
    handle.destroy();
  });

  it('shows default subtitle + welcome when not configured', () => {
    globalThis.fetch = happyFetch();
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x', inline: true });
    const sr = shadowOf();
    expect(sr.querySelector('.surg-subtitle')?.textContent).toBe('Typically replies instantly');
    expect(sr.querySelector('.surg-assistant')?.textContent).toContain('How can I help you today');
    handle.destroy();
  });

  it('update() changes appearance in place without re-mounting', async () => {
    globalThis.fetch = happyFetch();
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x', inline: true, title: 'Old' });
    const sr = shadowOf();
    expect(sr.querySelector('.surg-title')?.textContent).toBe('Old');
    handle.update({ title: 'New Co', accentColor: '#123456', subtitle: 'Here to help' });
    const root = sr.querySelector('.surg-root') as HTMLElement;
    // Subtitle + accent are direct reads — apply on the synchronous re-render.
    expect(sr.querySelector('.surg-subtitle')?.textContent).toBe('Here to help');
    expect(root.style.getPropertyValue('--surg-accent')).toBe('#123456');
    // Title is synced via an effect — settles on the next tick.
    await waitFor(() => sr.querySelector('.surg-title')?.textContent === 'New Co');
    // Same host element — not torn down and recreated.
    expect(document.querySelectorAll('[data-surogates-widget]').length).toBe(1);
    handle.destroy();
  });

  it('destroy() removes the host element', () => {
    globalThis.fetch = happyFetch();
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x', inline: true });
    expect(document.querySelector('[data-surogates-widget]')).toBeTruthy();
    handle.destroy();
    expect(document.querySelector('[data-surogates-widget]')).toBeNull();
  });
});

describe('mount() — send and stream', () => {
  it('sends a message and renders the streamed assistant reply', async () => {
    globalThis.fetch = happyFetch();
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x', inline: true });
    const sr = shadowOf();

    const input = sr.querySelector('.surg-input') as HTMLTextAreaElement;
    input.value = 'How do I reset my password?';
    input.dispatchEvent(new Event('input', { bubbles: true }));

    // The send button is disabled until the input-state rerender lands;
    // wait for it to enable so the click isn't dropped on a disabled button.
    const sendBtn = sr.querySelector('.surg-send') as HTMLButtonElement;
    await waitFor(() => !sendBtn.disabled);
    sendBtn.click();

    // User bubble echoes immediately.
    await waitFor(() => !!sr.querySelector('.surg-user'));
    expect(sr.querySelector('.surg-user')?.textContent).toContain('reset my password');

    // The agent opens an SSE stream for the run; drive a minimal turn.
    await waitFor(() => FakeEventSource.lastInstance !== undefined);
    const src = FakeEventSource.lastInstance as FakeEventSource;
    src.emit(SURG_EVENT.LLM_DELTA, { delta: 'Click ' }, 2);
    src.emit(SURG_EVENT.LLM_DELTA, { delta: '“Forgot password”.' }, 3);
    src.emit(
      SURG_EVENT.LLM_RESPONSE,
      { message: { content: 'Click “Forgot password”.' }, finish_reason: 'stop' },
      4,
    );

    await waitFor(() => /Forgot password/.test(sr.querySelector('.surg-assistant')?.textContent ?? ''));
    expect(sr.querySelector('.surg-assistant')?.textContent).toContain('Forgot password');
    handle.destroy();
  });

  it('shows no empty bubble while the agent works through tool calls', async () => {
    globalThis.fetch = happyFetch();
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x', inline: true });
    const sr = shadowOf();

    const input = sr.querySelector('.surg-input') as HTMLTextAreaElement;
    input.value = 'Find my order';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const sendBtn = sr.querySelector('.surg-send') as HTMLButtonElement;
    await waitFor(() => !sendBtn.disabled);
    sendBtn.click();
    await waitFor(() => FakeEventSource.lastInstance !== undefined);
    const src = FakeEventSource.lastInstance as FakeEventSource;

    const replies = () =>
      Array.from(sr.querySelectorAll('.surg-assistant:not(.surg-typing)')).map((el) => el.textContent ?? '');

    // A call with no text before it, then one after a footer-only reply.
    src.emit(SURG_EVENT.TOOL_CALL, { tool_call_id: 'tc-1', name: 'lookup', arguments: {} }, 2);
    src.emit(SURG_EVENT.TOOL_RESULT, { tool_call_id: 'tc-1', name: 'lookup', content: 'ok' }, 3);
    src.emit(SURG_EVENT.LLM_DELTA, { delta: '<next_action complexity="low" summary="x">y</next_action>' }, 4);
    src.emit(
      SURG_EVENT.LLM_RESPONSE,
      { message: { content: '<next_action complexity="low" summary="x">y</next_action>', tool_calls: [{}] }, finish_reason: 'tool_calls' },
      5,
    );
    src.emit(SURG_EVENT.TOOL_CALL, { tool_call_id: 'tc-2', name: 'lookup', arguments: {} }, 6);
    await waitFor(() => !!sr.querySelector('.surg-typing'));
    // Only the welcome bubble: nothing empty, and the typing dots stay up.
    expect(replies()).toHaveLength(1);
    expect(replies().every((text) => text.trim() !== '')).toBe(true);

    src.emit(SURG_EVENT.TOOL_RESULT, { tool_call_id: 'tc-2', name: 'lookup', content: 'ok' }, 7);
    src.emit(SURG_EVENT.LLM_RESPONSE, { message: { content: 'Your order ships today.' }, finish_reason: 'stop' }, 8);
    await waitFor(() => replies().some((text) => text.includes('ships today')));
    expect(replies().every((text) => text.trim() !== '')).toBe(true);
    expect(sr.querySelector('.surg-typing')).toBeNull();
    handle.destroy();
  });
});

describe('mount() — the agent asks a question', () => {
  async function startTurn(fetchStub: typeof fetch) {
    globalThis.fetch = fetchStub;
    const handle = mount({ apiUrl: 'https://a.test', publishableKey: 'surg_wk_x', inline: true });
    const sr = shadowOf();
    const input = sr.querySelector('.surg-input') as HTMLTextAreaElement;
    input.value = 'I want to sign up';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    const sendBtn = sr.querySelector('.surg-send') as HTMLButtonElement;
    await waitFor(() => !sendBtn.disabled);
    sendBtn.click();
    await waitFor(() => FakeEventSource.lastInstance !== undefined);
    return { handle, sr, src: FakeEventSource.lastInstance as FakeEventSource };
  }

  const answerCalls = (stub: ReturnType<typeof vi.fn>) =>
    stub.mock.calls.filter(([url]) => String(url).includes('/ask_user_question/'));

  it('renders the question with its choices and posts the one clicked', async () => {
    const stub = happyFetch() as unknown as ReturnType<typeof vi.fn>;
    const { handle, sr, src } = await startTurn(stub as unknown as typeof fetch);

    src.emit(
      SURG_EVENT.TOOL_CALL,
      {
        tool_call_id: 'tc-q',
        name: 'ask_user_question',
        arguments: {
          message: 'Happy to help.',
          questions: [{ prompt: 'Which plan?', choices: [{ label: 'Basic' }, { label: 'Pro', description: 'Most popular' }] }],
        },
      },
      2,
    );

    await waitFor(() => !!sr.querySelector('.surg-question'));
    const card = sr.querySelector('.surg-question') as HTMLElement;
    expect(card.textContent).toContain('Happy to help.');
    expect(card.textContent).toContain('Which plan?');
    const choices = Array.from(card.querySelectorAll('.surg-choice')) as HTMLButtonElement[];
    expect(choices.map((b) => b.textContent)).toEqual(['Basic', 'ProMost popular']);
    expect(card.querySelector('.surg-other')).toBeTruthy();
    expect(sr.querySelector('.surg-typing')).toBeNull();

    choices[1]!.click();
    await waitFor(() => answerCalls(stub).length === 1);
    const [url, init] = answerCalls(stub)[0] as [string, RequestInit];
    expect(url.split('?')[0]).toBe('https://a.test/v1/website/sessions/sess-1/ask_user_question/tc-q/respond');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-1');
    expect(JSON.parse(String(init.body))).toEqual({
      responses: [{ question: 'Which plan?', answer: 'Pro', is_other: false }],
    });

    await waitFor(() => choices.every((b) => b.disabled));
    expect(choices[1]!.classList.contains('surg-chosen')).toBe(true);
    expect(choices[0]!.classList.contains('surg-chosen')).toBe(false);

    src.emit(
      SURG_EVENT.TOOL_RESULT,
      {
        tool_call_id: 'tc-q',
        name: 'ask_user_question',
        content: JSON.stringify({ cancelled: false, responses: [{ question: 'Which plan?', answer: 'Pro', is_other: false }] }),
      },
      3,
    );
    await waitFor(() => !!sr.querySelector('.surg-typing'));
    expect(sr.querySelector('.surg-question .surg-chosen')?.textContent).toContain('Pro');
    handle.destroy();
  });

  it('takes a typed answer to an open question', async () => {
    const stub = happyFetch() as unknown as ReturnType<typeof vi.fn>;
    const { handle, sr, src } = await startTurn(stub as unknown as typeof fetch);

    src.emit(
      SURG_EVENT.TOOL_CALL,
      { tool_call_id: 'tc-o', name: 'ask_user_question', arguments: JSON.stringify({ questions: [{ prompt: 'Your email?' }] }) },
      2,
    );
    await waitFor(() => !!sr.querySelector('.surg-other'));
    const other = sr.querySelector('.surg-other') as HTMLInputElement;
    other.value = 'ana@example.com';
    other.dispatchEvent(new Event('input', { bubbles: true }));
    const submit = sr.querySelector('.surg-question-send') as HTMLButtonElement;
    await waitFor(() => !submit.disabled);
    submit.click();

    await waitFor(() => answerCalls(stub).length === 1);
    expect(JSON.parse(String((answerCalls(stub)[0] as [string, RequestInit])[1].body))).toEqual({
      responses: [{ question: 'Your email?', answer: 'ana@example.com', is_other: false }],
    });
    await waitFor(() => other.disabled);
    handle.destroy();
  });
});
