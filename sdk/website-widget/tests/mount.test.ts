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
    if (init?.method === 'POST' && !String(url).includes('/messages')) {
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
    src.emit(SURG_EVENT.LLM_DELTA, { delta: 'Click ' }, 1);
    src.emit(SURG_EVENT.LLM_DELTA, { delta: '“Forgot password”.' }, 2);
    src.emit(SURG_EVENT.LLM_RESPONSE, { content: 'Click “Forgot password”.', finish_reason: 'stop' }, 3);

    await waitFor(() => /Forgot password/.test(sr.querySelector('.surg-assistant')?.textContent ?? ''));
    expect(sr.querySelector('.surg-assistant')?.textContent).toContain('Forgot password');
    handle.destroy();
  });
});
