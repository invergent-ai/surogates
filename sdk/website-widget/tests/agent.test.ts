/**
 * Unit tests for :class:`WebsiteAgent` -- the AG-UI bridge class.
 *
 * These run entirely in-process: ``fetch`` is stubbed via
 * ``globalThis.fetch`` and ``EventSource`` via a ``FakeEventSource``
 * class.  We deliberately avoid a real HTTP server -- the goal is to
 * lock down the observable-lifecycle invariants (RUN_STARTED / ...
 * events / RUN_FINISHED or RUN_ERROR) and the two failure paths the
 * reviewer called out: cookie-expiry recovery and SSE terminal close.
 *
 * happy-dom provides the DOM globals (``fetch``, ``Event``,
 * ``MessageEvent``) that our real code consumes; we shadow them with
 * per-test stubs so the assertions stay deterministic.
 */

import type { BaseEvent, RunAgentInput } from '@ag-ui/client';
import { EventType } from '@ag-ui/client';
import type { Subscription } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebsiteAgent } from '../src/agent.js';
import { SURG_EVENT } from '../src/constants.js';

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/**
 * Minimal ``EventSource`` stand-in that records the URL it was opened
 * against and exposes ``emit`` / ``close`` for tests to drive the
 * agent's state machine deterministically.  Captures the current
 * "last instance" so individual tests can reach the active stream
 * without threading references through the agent.
 */
class FakeEventSource {
  static lastInstance: FakeEventSource | undefined;
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState = 1; // OPEN
  onerror: ((ev: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<(ev: MessageEvent) => void>>();

  constructor(url: string, _init?: EventSourceInit) {
    this.url = url;
    FakeEventSource.lastInstance = this;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, fn: (ev: MessageEvent) => void): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(fn);
  }

  removeEventListener(type: string, fn: (ev: MessageEvent) => void): void {
    this.listeners.get(type)?.delete(fn);
  }

  close(): void {
    this.readyState = 2; // CLOSED
  }

  /** Dispatch a synthetic Surogates SSE frame to the subscribed listeners.
   *
   * A closed EventSource (``readyState === 2``) silently drops events
   * in real browsers -- the internal listener list is still populated
   * but the event loop never pulls from it.  The fake mirrors that
   * behaviour so tests catch regressions where the production code
   * closes the source too early (any event emitted post-close would
   * otherwise reach the listeners and pass assertions that only hold
   * because the fake was over-permissive).
   */
  emit(eventName: string, data: unknown, id: number): void {
    if (this.readyState === 2) return;
    const set = this.listeners.get(eventName);
    if (!set) return;
    const ev = new MessageEvent(eventName, {
      data: JSON.stringify(data),
      lastEventId: String(id),
    });
    for (const fn of Array.from(set)) fn(ev);
  }

  /** Simulate the browser firing ``onerror`` with a terminal close. */
  fail(): void {
    this.readyState = 2;
    this.onerror?.(new Event('error'));
  }

  static reset(): void {
    FakeEventSource.lastInstance = undefined;
    FakeEventSource.instances = [];
  }
}

/**
 * Minimal ``Response`` factory.  ``happy-dom`` ships a spec-compliant
 * ``Response`` but assembling one is awkward for the fields we care
 * about; this helper lets a test hand the stub a ``status`` + body
 * and have the protocol layer interpret it correctly.
 */
function makeResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  const h = new Headers(headers);
  const init: ResponseInit = { status, headers: h };
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), init);
}

/** Collect events from an agent.run() observable into an array. */
function collectEvents(observable: ReturnType<WebsiteAgent['run']>): {
  events: BaseEvent[];
  done: Promise<void>;
  sub: Subscription;
} {
  const events: BaseEvent[] = [];
  let resolveDone: () => void;
  let rejectDone: (err: unknown) => void;
  const done = new Promise<void>((res, rej) => {
    resolveDone = res;
    rejectDone = rej;
  });
  const sub = observable.subscribe({
    next: (e: BaseEvent) => events.push(e),
    error: (err: unknown) => rejectDone(err),
    complete: () => resolveDone(),
  });
  return { events, done, sub };
}

const INPUT: RunAgentInput = {
  threadId: 't-1',
  runId: 'r-1',
  messages: [{ id: 'u1', role: 'user', content: 'hello' }],
  tools: [],
  context: [],
  state: {},
  forwardedProps: {},
};

// ---------------------------------------------------------------------------
// Global fetch + EventSource wiring
// ---------------------------------------------------------------------------

const originalFetch = globalThis.fetch;
const originalEventSource = globalThis.EventSource;

beforeEach(() => {
  FakeEventSource.reset();
  (globalThis as unknown as { EventSource: typeof FakeEventSource }).EventSource =
    FakeEventSource;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    originalEventSource;
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('WebsiteAgent -- constructor', () => {
  it('throws when apiUrl is missing', () => {
    expect(() => new WebsiteAgent({ apiUrl: '', publishableKey: 'surg_wk_x' })).toThrow(
      /apiUrl/,
    );
  });

  it('throws when publishableKey is missing', () => {
    expect(
      () => new WebsiteAgent({ apiUrl: 'https://api.test', publishableKey: '' }),
    ).toThrow(/publishableKey/);
  });

  it('strips trailing slashes from apiUrl', () => {
    const a = new WebsiteAgent({
      apiUrl: 'https://api.test///',
      publishableKey: 'surg_wk_x',
    });
    expect(a.apiUrl).toBe('https://api.test');
  });
});

describe('WebsiteAgent -- happy path', () => {
  it('emits RUN_STARTED, translated frames, RUN_FINISHED in order', async () => {
    const fetchMock = vi.fn(async (_url: unknown, init: RequestInit | undefined) => {
      // Two calls: bootstrap (POST /v1/website/sessions) and sendMessage.
      if (init?.method === 'POST' && !String(_url).includes('/messages')) {
        return makeResponse(201, {
          session_id: 'sess-1',
          csrf_token: 'csrf-1',
          expires_at: Date.now() / 1000 + 3600,
          agent_name: 'support-bot',
        });
      }
      return makeResponse(202, { event_id: 42 });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const agent = new WebsiteAgent({
      apiUrl: 'https://api.test',
      publishableKey: 'surg_wk_demo',
    });
    const { events, done } = collectEvents(agent.run(INPUT));

    // Wait a microtask cycle so the async driveRun has attached
    // stream listeners before we start emitting.  The order is
    // bootstrap → open SSE → send; all three awaits must settle
    // before the synthetic stream is listening.
    await waitForSource();
    const src = requireSource();
    expect(src.url).toBe('https://api.test/v1/website/sessions/sess-1/events?after=0');

    // Simulate a minimal successful turn.
    src.emit(SURG_EVENT.LLM_DELTA, { delta: 'hi' }, 1);
    src.emit(SURG_EVENT.LLM_RESPONSE, { content: 'hi', finish_reason: 'stop' }, 2);

    await done;

    const types = events.map((e) => e.type);
    expect(types[0]).toBe(EventType.RUN_STARTED);
    expect(types[types.length - 1]).toBe(EventType.RUN_FINISHED);
    expect(types).toContain(EventType.TEXT_MESSAGE_CHUNK);
    // Exactly one terminal event.
    const terminals = types.filter(
      (t) => t === EventType.RUN_FINISHED || t === EventType.RUN_ERROR,
    );
    expect(terminals).toEqual([EventType.RUN_FINISHED]);
  });

  it('advances the cursor past frames arriving during the drain window', async () => {
    // Critical guard for the issue #1 fix: after the translator flips
    // end-of-turn, a trailing ``memory.update`` must advance cursor
    // so the NEXT run's ``?after=`` skips it.  Without the drain
    // window those frames replay as spurious CUSTOM events.
    vi.useFakeTimers();
    globalThis.fetch = vi.fn(async (_u: unknown, init: RequestInit | undefined) => {
      if (init?.method === 'POST' && !String(_u).includes('/messages')) {
        return makeResponse(201, {
          session_id: 's',
          csrf_token: 'c',
          expires_at: 0,
          agent_name: 'a',
        });
      }
      return makeResponse(202, { event_id: 1 });
    }) as unknown as typeof fetch;

    const agent = new WebsiteAgent({ apiUrl: 'https://api.test', publishableKey: 'k' });
    const { done } = collectEvents(agent.run(INPUT));

    await waitForSource();
    const src = requireSource();
    src.emit(SURG_EVENT.LLM_RESPONSE, { content: 'ok', finish_reason: 'stop' }, 10);
    await done;

    // Trailing frame during the drain window.
    src.emit(SURG_EVENT.MEMORY_UPDATE, { action: 'add' }, 11);
    // Fast-forward past the drain window so the close() fires.
    vi.advanceTimersByTime(1000);

    // Cursor should have moved past 11, not stuck at 10.  Private
    // field access via bracket notation avoids exposing the cursor
    // in the public API while still allowing the invariant to be
    // asserted.
    const cursor = (agent as unknown as { cursor: number }).cursor;
    expect(cursor).toBe(11);
    vi.useRealTimers();
  });
});

describe('WebsiteAgent -- cookie-expiry recovery', () => {
  it('re-bootstraps once on 401 and retries the send against the fresh session', async () => {
    let bootstrapCalls = 0;
    let sendCalls = 0;
    globalThis.fetch = vi.fn(async (url: unknown, init: RequestInit | undefined) => {
      const u = String(url);
      if (init?.method === 'POST' && u.endsWith('/v1/website/sessions')) {
        bootstrapCalls += 1;
        return makeResponse(201, {
          session_id: `sess-${bootstrapCalls}`,
          csrf_token: `csrf-${bootstrapCalls}`,
          expires_at: 0,
          agent_name: 'a',
        });
      }
      if (u.includes('/messages')) {
        sendCalls += 1;
        // First send 401s (cookie expired); retry on fresh session succeeds.
        if (sendCalls === 1) {
          return makeResponse(401, { detail: 'cookie expired' });
        }
        return makeResponse(202, { event_id: 99 });
      }
      throw new Error(`unexpected fetch: ${u}`);
    }) as unknown as typeof fetch;

    const agent = new WebsiteAgent({ apiUrl: 'https://api.test', publishableKey: 'k' });
    const { events, done } = collectEvents(agent.run(INPUT));

    await waitForSource();
    // Eventually the retry finishes, opens a second EventSource
    // against sess-2, and we need to drive that one to a clean
    // end-of-turn so ``done`` resolves.  Spin on the observable
    // side-effects (bootstrap call count, instance count) rather
    // than a fixed microtask budget: happy-dom's fetch resolves
    // across several microtask hops so the retry flow needs > 10.
    await waitUntil(() => bootstrapCalls === 2 && sendCalls === 2);
    await waitUntil(() => FakeEventSource.instances.length === 2);

    // The active source should be the one opened against sess-2
    // (the fresh session from the retry).
    const active = requireSource();
    expect(active.url).toContain('/sessions/sess-2/events');
    active.emit(SURG_EVENT.LLM_RESPONSE, { content: 'ok', finish_reason: 'stop' }, 1);
    await done;

    // Exactly one RUN_STARTED and one RUN_FINISHED (no RUN_ERROR
    // leaked from the discarded old source).
    const types = events.map((e) => e.type);
    expect(types.filter((t) => t === EventType.RUN_STARTED)).toHaveLength(1);
    expect(types.filter((t) => t === EventType.RUN_FINISHED)).toHaveLength(1);
    expect(types).not.toContain(EventType.RUN_ERROR);
  });

  it('detaches the old SSE onerror so it cannot race a terminal event', async () => {
    // Paired with the fix above: during recovery we null the old
    // source's onerror before calling close(), so any late browser
    // transition to ``readyState=2`` on the discarded source can't
    // emit a second RUN_ERROR.
    let bootstrapCalls = 0;
    let sendCalls = 0;
    globalThis.fetch = vi.fn(async (url: unknown, init: RequestInit | undefined) => {
      const u = String(url);
      if (init?.method === 'POST' && u.endsWith('/v1/website/sessions')) {
        bootstrapCalls += 1;
        return makeResponse(201, {
          session_id: `sess-${bootstrapCalls}`,
          csrf_token: `csrf-${bootstrapCalls}`,
          expires_at: 0,
          agent_name: 'a',
        });
      }
      sendCalls += 1;
      return sendCalls === 1
        ? makeResponse(401, { detail: 'expired' })
        : makeResponse(202, { event_id: 1 });
    }) as unknown as typeof fetch;

    const agent = new WebsiteAgent({ apiUrl: 'https://api.test', publishableKey: 'k' });
    const { events, done } = collectEvents(agent.run(INPUT));
    await waitForSource();
    await waitUntil(() => bootstrapCalls === 2 && sendCalls === 2);
    await waitUntil(() => FakeEventSource.instances.length === 2);

    const [stale, active] = FakeEventSource.instances;
    if (!stale || !active) throw new Error('expected both stale and active sources');

    // The stale source's onerror should be null -- the ``closeEventSource``
    // helper nulls it out before close() so a late transition to
    // CLOSED can't sneak a second RUN_ERROR through.
    expect(stale.onerror).toBeNull();

    active.emit(SURG_EVENT.LLM_RESPONSE, { content: 'ok', finish_reason: 'stop' }, 1);
    await done;

    const types = events.map((e) => e.type);
    expect(types).not.toContain(EventType.RUN_ERROR);
  });
});

describe('WebsiteAgent -- SSE terminal close', () => {
  it('emits RUN_ERROR with code=sse_closed when the EventSource closes', async () => {
    globalThis.fetch = vi.fn(async (_u: unknown, init: RequestInit | undefined) => {
      if (init?.method === 'POST' && !String(_u).includes('/messages')) {
        return makeResponse(201, {
          session_id: 's',
          csrf_token: 'c',
          expires_at: 0,
          agent_name: 'a',
        });
      }
      return makeResponse(202, { event_id: 1 });
    }) as unknown as typeof fetch;

    const agent = new WebsiteAgent({ apiUrl: 'https://api.test', publishableKey: 'k' });
    const { events, done } = collectEvents(agent.run(INPUT));
    await waitForSource();

    // Simulate a terminal transport drop.
    requireSource().fail();
    await done;

    const final = events[events.length - 1] as { type: EventType; code?: string };
    expect(final.type).toBe(EventType.RUN_ERROR);
    expect(final.code).toBe('sse_closed');
  });
});

describe('WebsiteAgent -- lifecycle guarantees', () => {
  it('emits exactly one RUN_STARTED and one terminal event across errors', async () => {
    // Auth error on bootstrap -- the most surface-of-attack path for
    // the double-terminate bug the fix guards against.
    globalThis.fetch = vi.fn(async () =>
      makeResponse(403, { detail: 'origin not allowed' }),
    ) as unknown as typeof fetch;

    const agent = new WebsiteAgent({ apiUrl: 'https://api.test', publishableKey: 'k' });
    const { events, done } = collectEvents(agent.run(INPUT));
    await done;

    const types = events.map((e) => e.type);
    expect(types.filter((t) => t === EventType.RUN_STARTED)).toHaveLength(1);
    const terminalCount = types.filter(
      (t) => t === EventType.RUN_FINISHED || t === EventType.RUN_ERROR,
    ).length;
    expect(terminalCount).toBe(1);
    expect(types).toContain(EventType.RUN_ERROR);
  });

  it('emits RUN_ERROR when no user message is present', async () => {
    const agent = new WebsiteAgent({ apiUrl: 'https://api.test', publishableKey: 'k' });
    const noUser: RunAgentInput = { ...INPUT, messages: [] };
    const { events, done } = collectEvents(agent.run(noUser));
    await done;
    const types = events.map((e) => e.type);
    expect(types[0]).toBe(EventType.RUN_STARTED);
    expect(types[types.length - 1]).toBe(EventType.RUN_ERROR);
  });
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Wait up to ``attempts`` microtask cycles for a FakeEventSource to
 * be constructed.  The agent's ``driveRun`` is async; the test can't
 * deterministically ``await`` on it so we spin until the side-effect
 * we're waiting for is observable.
 */
async function waitForSource(attempts = 100): Promise<void> {
  await waitUntil(() => FakeEventSource.lastInstance !== undefined, attempts);
}

/**
 * Return the current active EventSource or fail the test.  Using this
 * helper means individual test assertions don't have to annotate
 * non-null assumptions at every call site -- the helper name carries
 * that intent.
 */
function requireSource(): FakeEventSource {
  const src = FakeEventSource.lastInstance;
  if (!src) throw new Error('expected a live FakeEventSource');
  return src;
}

/**
 * Spin until ``predicate`` returns true or we run out of attempts.
 * Each iteration yields to the microtask queue (one ``await
 * Promise.resolve()``) so fetch mock resolutions and intervening
 * ``await`` points make progress.  Used to wait for the two-hop
 * cookie-recovery dance (1st bootstrap → 1st send fails → 2nd
 * bootstrap → 2nd send) which can take 20+ microtask cycles.
 */
async function waitUntil(predicate: () => boolean, attempts = 100): Promise<void> {
  for (let i = 0; i < attempts; i++) {
    if (predicate()) return;
    await Promise.resolve();
  }
  throw new Error('waitUntil predicate never became true');
}
