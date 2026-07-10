/**
 * Paywall (HTTP 402) handling across the wire layer and the agent.
 *
 * The server refuses monetized-agent messages with a structured 402
 * detail (``{code, buy_url}``); the SDK must surface it as the typed
 * :class:`SurogatesPaywallError`, the agent must translate it into a
 * ``RUN_ERROR`` with ``code: 'paywall'`` and expose the structured
 * sentinel on ``lastPaywall``, and the bootstrap must carry the
 * embedder-supplied Firebase ID token.
 */

import { EventType } from '@ag-ui/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WebsiteAgent } from '../src/agent.js';
import { SurogatesPaywallError } from '../src/errors.js';
import { bootstrap, sendMessage } from '../src/protocol.js';

const originalFetch = globalThis.fetch;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('protocol 402 handling', () => {
  it('maps a structured 402 detail to SurogatesPaywallError', async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(402, {
        detail: {
          code: 'insufficient_tokens',
          buy_url: 'https://studio.example/buy/helper',
        },
      }),
    ) as unknown as typeof fetch;

    const err = await sendMessage('https://api', 's-1', 'csrf', 'hi').catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(SurogatesPaywallError);
    const paywall = err as SurogatesPaywallError;
    expect(paywall.status).toBe(402);
    expect(paywall.code).toBe('insufficient_tokens');
    expect(paywall.buyUrl).toBe('https://studio.example/buy/helper');
  });

  it('maps a plain-string 402 detail to a paywall error too', async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(402, { detail: 'subscription_required' }),
    ) as unknown as typeof fetch;

    const err = await sendMessage('https://api', 's-1', 'csrf', 'hi').catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(SurogatesPaywallError);
    expect((err as SurogatesPaywallError).code).toBe('subscription_required');
    expect((err as SurogatesPaywallError).buyUrl).toBeUndefined();
  });

  it('falls back to payment_required on a bodyless 402', async () => {
    globalThis.fetch = vi.fn(
      async () => new Response('', { status: 402 }),
    ) as unknown as typeof fetch;

    const err = await sendMessage('https://api', 's-1', 'csrf', 'hi').catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(SurogatesPaywallError);
    expect((err as SurogatesPaywallError).code).toBe('payment_required');
  });
});

describe('bootstrap firebase token pass-through', () => {
  const bootBody = {
    session_id: 's-1',
    csrf_token: 'csrf',
    expires_at: 123,
    agent_name: 'helper',
  };

  it('sends the token as the bootstrap body when supplied', async () => {
    const fetchMock = vi.fn(
      async (_url: unknown, _init?: RequestInit) => jsonResponse(201, bootBody),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await bootstrap('https://api', 'surg_wk_k', undefined, 'fb-id-token');

    const init = fetchMock.mock.calls[0]?.[1];
    expect(JSON.parse(String(init?.body))).toEqual({
      firebase_id_token: 'fb-id-token',
    });
  });

  it('sends no body at all for anonymous bootstraps', async () => {
    const fetchMock = vi.fn(
      async (_url: unknown, _init?: RequestInit) => jsonResponse(201, bootBody),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await bootstrap('https://api', 'surg_wk_k');

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.body).toBeUndefined();
  });
});

describe('WebsiteAgent paywall run flow', () => {
  beforeEach(() => {
    // The agent opens an SSE stream before sending; a inert stub keeps
    // the run driving into sendMessage where the 402 fires.
    class StubEventSource {
      onmessage: unknown = null;
      onerror: unknown = null;
      constructor(public url: string) {}
      addEventListener() {}
      close() {}
    }
    vi.stubGlobal('EventSource', StubEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('emits RUN_ERROR code paywall and records lastPaywall', async () => {
    const bootBody = {
      session_id: 's-1',
      csrf_token: 'csrf',
      expires_at: 123,
      agent_name: 'helper',
    };
    globalThis.fetch = vi.fn(async (url: unknown) => {
      if (String(url).includes('/messages')) {
        return jsonResponse(402, {
          detail: {
            code: 'sign_in_required',
            buy_url: 'https://studio.example/buy/helper',
          },
        });
      }
      return jsonResponse(201, bootBody);
    }) as unknown as typeof fetch;

    const agent = new WebsiteAgent({
      apiUrl: 'https://api',
      publishableKey: 'surg_wk_k',
      getFirebaseIdToken: () => null,
    });

    const events: Array<{ type: string; code?: string }> = [];
    await new Promise<void>((resolve) => {
      agent
        .run({
          threadId: 't-1',
          runId: 'r-1',
          messages: [{ id: 'm-1', role: 'user', content: 'hi' }],
          state: {},
          tools: [],
          context: [],
          forwardedProps: {},
        })
        .subscribe({
          next: (event) => events.push(event as { type: string; code?: string }),
          error: () => resolve(),
          complete: () => resolve(),
        });
    });

    const runError = events.find((e) => e.type === EventType.RUN_ERROR);
    expect(runError).toBeDefined();
    expect(runError?.code).toBe('paywall');
    expect(agent.lastPaywall).toEqual({
      code: 'sign_in_required',
      buyUrl: 'https://studio.example/buy/helper',
    });
  });

  it('drops the cached anonymous session on sign_in_required so the next send re-bootstraps with a token', async () => {
    const bootBody = {
      session_id: 's-1',
      csrf_token: 'csrf',
      expires_at: 123,
      agent_name: 'helper',
    };
    const bootstrapBodies: Array<string | undefined> = [];
    globalThis.fetch = vi.fn(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes('/messages')) {
        return jsonResponse(402, { detail: { code: 'sign_in_required' } });
      }
      bootstrapBodies.push(init?.body ? String(init.body) : undefined);
      return jsonResponse(201, bootBody);
    }) as unknown as typeof fetch;

    let token: string | null = null;
    const agent = new WebsiteAgent({
      apiUrl: 'https://api',
      publishableKey: 'surg_wk_k',
      getFirebaseIdToken: () => token,
    });

    const runOnce = () =>
      new Promise<void>((resolve) => {
        agent
          .run({
            threadId: 't-1',
            runId: 'r-1',
            messages: [{ id: 'm-1', role: 'user', content: 'hi' }],
            state: {},
            tools: [],
            context: [],
            forwardedProps: {},
          })
          .subscribe({ error: () => resolve(), complete: () => resolve() });
      });

    await runOnce();
    expect(bootstrapBodies).toEqual([undefined]);

    token = 'fb-token';
    await runOnce();
    expect(bootstrapBodies).toHaveLength(2);
    expect(JSON.parse(String(bootstrapBodies[1]))).toEqual({
      firebase_id_token: 'fb-token',
    });
  });
});
