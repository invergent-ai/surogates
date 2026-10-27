/**
 * AG-UI ``AbstractAgent`` implementation for the Surogates public-website channel.
 *
 * A ``WebsiteAgent`` bridges the channel-specific HTTP protocol
 * (publishable-key bootstrap, HttpOnly cookie, CSRF double-submit,
 * ``/v1/website/sessions/{id}/events`` SSE) onto AG-UI's standard
 * ``run()`` contract so any AG-UI-aware host app -- CopilotKit,
 * custom chat UIs, LangGraph-style clients -- can talk to a Surogates
 * backend with no custom glue beyond instantiation.
 *
 * Responsibilities
 * ----------------
 * * **Lazy bootstrap.**  The first ``runAgent`` call POSTs to
 *   ``/v1/website/sessions`` to exchange the publishable key for a
 *   session cookie + CSRF token; subsequent calls reuse them.  A cookie
 *   expiry (HTTP 401) triggers a transparent re-bootstrap so the host
 *   app never has to manage auth state.
 *
 * * **Message → POST.**  On each ``runAgent`` call the agent finds the
 *   latest user message in ``input.messages`` and posts its content to
 *   ``/v1/website/sessions/{id}/messages`` with the CSRF header.
 *
 * * **SSE → AG-UI.**  The channel's native event stream is converted
 *   to AG-UI events by :class:`Translator` frame-by-frame.  The agent
 *   opens a fresh EventSource per run once its message is posted, at
 *   ``?after=<the message's event id>`` so earlier turns never replay,
 *   and closes it after a short grace drain on turn completion.  When
 *   the server ends a stream (``stream.timeout``) or the transport
 *   drops while the run is live, the agent reopens it from its cursor,
 *   backing off between failed attempts, and drops any frame it has
 *   already seen.
 *
 * * **RUN lifecycle.**  AG-UI requires every run to be bracketed by
 *   ``RUN_STARTED`` and exactly one of ``RUN_FINISHED`` / ``RUN_ERROR``.
 *   Surogates has no such sentinel on the wire, so the agent emits
 *   them itself; a per-run ``terminated`` flag guarantees at most one
 *   terminal event even under concurrent SSE ``onerror`` + translator
 *   end-of-turn detection.
 */

import type {
  AgentConfig,
  BaseEvent,
  Message,
  RunAgentInput,
} from '@ag-ui/client';
import { AbstractAgent, AGUIError, EventType } from '@ag-ui/client';
import type { Subscriber } from 'rxjs';
import { Observable } from 'rxjs';

import {
  type BootstrapResult,
  bootstrap,
  endSession,
  openEventStream,
  sendMessage,
} from './protocol.js';
import { type SurogatesFrame, Translator } from './translator.js';
import { SurogatesAuthError, SurogatesPaywallError } from './errors.js';
import { SURG_EVENT } from './constants.js';

/**
 * Constructor config for :class:`WebsiteAgent`.  Extends AG-UI's
 * base ``AgentConfig`` with the two Surogates-specific fields the
 * bootstrap needs; everything else (``agentId``, ``threadId``,
 * ``initialMessages``, ``initialState``, ``debug``) is inherited
 * unchanged so consumers reuse the AG-UI patterns they already know.
 */
export interface WebsiteAgentConfig extends AgentConfig {
  /** Base URL of the Surogates API, e.g. ``https://agent.acme.com``. */
  apiUrl: string;
  /** Publishable key issued by ops (``surg_wk_...``). */
  publishableKey: string;
  /**
   * Supplies the signed-in visitor's Firebase ID token at bootstrap.
   * Required to chat with monetized agents: sites that authenticate
   * end users against the agent project's Firebase pass a getter
   * here, the SDK sends the token with the session bootstrap, and the
   * server binds the verified buyer identity to the visitor session.
   * Returning ``null``/``undefined`` (or omitting the hook) keeps the
   * session anonymous — monetized agents then answer with a
   * ``sign_in_required`` paywall instead of replies.
   */
  getFirebaseIdToken?: () =>
    | Promise<string | null | undefined>
    | string
    | null
    | undefined;
}

/** Set of Surogates SSE event names the agent subscribes to by default. */
const SURG_EVENT_NAMES: readonly string[] = Object.values(SURG_EVENT);

/**
 * How long to keep the EventSource open after the translator flags
 * end-of-turn, giving the server a window to deliver trailing frames
 * (late ``memory.update``, ``session.done``, etc.) that belong to the
 * closing run rather than the next one.  During this window we still
 * advance ``this.cursor`` but no longer emit to the subscriber, which
 * preserves AG-UI's "one terminal event per run" contract while also
 * preventing replay of those frames as spurious ``CUSTOM`` events on
 * the next run.
 */
const DRAIN_WINDOW_MS = 250;

/**
 * Waits before each reopen of a dropped stream.  A frame received
 * resets the count, so only consecutive failures exhaust it.
 */
const RECONNECT_DELAYS_MS: readonly number[] = [1000, 2000, 4000, 4000, 4000];

/**
 * Frames the server sends without an ``id:`` line.  A browser hands
 * them the stream's previous id as ``lastEventId``, so they must not be
 * mistaken for a replay of the frame that carried it.
 */
const UNNUMBERED_FRAMES: ReadonlySet<string> = new Set([
  SURG_EVENT.SESSION_DONE,
  SURG_EVENT.STREAM_TIMEOUT,
]);

export class WebsiteAgent extends AbstractAgent {
  readonly apiUrl: string;
  readonly publishableKey: string;

  /**
   * Details of the last paywall refusal (RUN_ERROR ``code:
   * 'paywall'``).  The AG-UI ``RUN_ERROR`` event only carries a
   * message + code string, so the structured sentinel and buy URL are
   * exposed here for UIs to read when they receive that code.
   */
  lastPaywall: { code: string; buyUrl?: string } | undefined;

  /** Cached bootstrap result.  ``undefined`` until the first ``run()``. */
  private bootstrapResult: BootstrapResult | undefined;
  /** Monotonic cursor across runs.  Passed to the SSE ``?after=`` param. */
  private cursor = 0;
  private readonly getFirebaseIdToken: WebsiteAgentConfig['getFirebaseIdToken'];

  constructor(config: WebsiteAgentConfig) {
    super(config);
    if (!config.apiUrl) {
      throw new AGUIError('WebsiteAgent: "apiUrl" is required.');
    }
    if (!config.publishableKey) {
      throw new AGUIError('WebsiteAgent: "publishableKey" is required.');
    }
    // Strip a trailing slash so ``${apiUrl}/v1/...`` never double-slashes.
    this.apiUrl = config.apiUrl.replace(/\/+$/, '');
    this.publishableKey = config.publishableKey;
    this.getFirebaseIdToken = config.getFirebaseIdToken;
  }

  /**
   * Ensure we have a live session cookie + CSRF token.
   *
   * Idempotent; returns the cached result when one already exists.
   * ``bootstrap()`` is the only call that requires the publishable
   * key to travel in an Authorization header, so callers that want
   * to eagerly validate their configuration (e.g. to surface a
   * setup error at widget-load time) can invoke this directly.
   */
  async ensureBootstrapped(): Promise<BootstrapResult> {
    if (this.bootstrapResult) return this.bootstrapResult;
    // Resolved per bootstrap (not cached) so cookie-expiry recovery
    // picks up a token the visitor acquired after the first attempt.
    const firebaseIdToken = this.getFirebaseIdToken
      ? await this.getFirebaseIdToken()
      : undefined;
    this.bootstrapResult = await bootstrap(
      this.apiUrl,
      this.publishableKey,
      this.agentId,
      firebaseIdToken ?? undefined,
    );
    return this.bootstrapResult;
  }

  /**
   * Close the current session on the server side and drop local state.
   *
   * Callers should invoke this when the visitor closes the chat UI
   * so the server can mark the session complete instead of waiting
   * for the idle-reset timer.  Safe to call when no session has been
   * bootstrapped yet.
   */
  async end(): Promise<void> {
    const current = this.bootstrapResult;
    if (!current) return;
    this.bootstrapResult = undefined;
    this.cursor = 0;
    await endSession(this.apiUrl, current.sessionId, current.csrfToken, this.agentId);
  }

  /**
   * Implementation of the AG-UI ``run()`` contract.
   *
   * Returns a cold observable: one subscription per run.  AG-UI's
   * ``runAgent`` pipeline pipes this through chunk transformation,
   * verification, and subscriber notifications before updating
   * ``this.messages``.
   */
  run(input: RunAgentInput): Observable<BaseEvent> {
    return new Observable<BaseEvent>((subscriber) => {
      // Per-run mutable state owned by the factory closure.  The
      // async ``driveRun`` populates ``ctx.source`` once the SSE is
      // open; the teardown at the bottom accesses whichever source
      // is current (swapped on cookie-recovery) and closes it.
      const ctx: RunContext = {
        terminated: false,
        source: undefined,
        drainTimer: undefined,
        reconnectTimer: undefined,
        failedReconnects: 0,
      };

      void this.driveRun(input, subscriber, ctx).catch((err) => {
        // Any rejection from driveRun that wasn't already turned into
        // a RUN_ERROR is a genuine bug; surface it through the RxJS
        // error channel rather than swallow.
        if (!ctx.terminated) {
          ctx.terminated = true;
          subscriber.error(err);
        }
      });

      return () => {
        // RxJS invokes this teardown on ``subscriber.complete()``,
        // ``subscriber.error()``, and on external ``unsubscribe()``
        // (whichever fires first).  Distinguishing them matters
        // because ``finish()`` wants the EventSource to stay OPEN
        // for the drain window -- cursor advancement relies on
        // trailing frames still arriving through the listeners.
        //
        // Rule: if there's a drain timer pending, leave the source
        // alone; the timer will close it.  Otherwise (error, abort)
        // close immediately.  Either way, mark terminated so any
        // in-flight frame callbacks skip subscriber emission.
        ctx.terminated = true;
        if (ctx.reconnectTimer !== undefined) {
          globalThis.clearTimeout(ctx.reconnectTimer);
          ctx.reconnectTimer = undefined;
        }
        if (ctx.drainTimer !== undefined) return;
        closeEventSource(ctx.source);
        ctx.source = undefined;
      };
    });
  }

  /**
   * Async body of the observable factory.
   *
   * Emits ``RUN_STARTED`` up front so every downstream failure is
   * well-bracketed by the AG-UI lifecycle.  ``RUN_FINISHED`` is
   * emitted by the SSE listeners when the translator detects end-of-
   * turn; ``RUN_ERROR`` is emitted from the catch below when bootstrap
   * or message-send fails before the SSE has any chance to close out.
   *
   * The ``ctx.terminated`` guard is the single enforcement point for
   * AG-UI's "exactly one terminal event" invariant.  Every emission
   * site checks it before calling ``subscriber.next`` with RUN_FINISHED
   * or RUN_ERROR, and every terminal call sets it to true first.
   */
  private async driveRun(
    input: RunAgentInput,
    subscriber: Subscriber<BaseEvent>,
    ctx: RunContext,
  ): Promise<void> {
    if (ctx.terminated) return;
    subscriber.next({
      type: EventType.RUN_STARTED,
      threadId: input.threadId,
      runId: input.runId,
    } as BaseEvent);

    try {
      const userText = this.extractUserText(input.messages);
      const boot = await this.ensureBootstrapped();

      // POST the visitor's message (one retry on 401 via a re-bootstrap),
      // then stream from the message's own event id.  Events are stored
      // before they are published, so a stream opened after the POST
      // replays whatever the turn already wrote and misses nothing, while
      // everything at or before the message belongs to earlier turns: a
      // page reload (cursor 0 on a reused session) or a run that ended
      // early must not feed old replies into this run's translator.
      // Browsers drop Authorization headers on EventSource, but the
      // session cookie rides along (the route authenticates off the
      // cookie + the Origin re-check).
      const sent = await this.sendWithCookieRecovery(boot, userText);
      if (ctx.terminated) return;
      this.cursor = Math.max(this.cursor, sent.eventId);
      this.replaceStreamSource(ctx, sent.sessionId, new Translator(), subscriber, input);
    } catch (err) {
      if (ctx.terminated) return;
      ctx.terminated = true;
      const message = err instanceof Error ? err.message : String(err);
      let code = 'error';
      if (err instanceof SurogatesAuthError) {
        code = 'auth';
      } else if (err instanceof SurogatesPaywallError) {
        code = 'paywall';
        this.lastPaywall = {
          code: err.code,
          ...(err.buyUrl !== undefined && { buyUrl: err.buyUrl }),
        };
        if (err.code === 'sign_in_required') {
          // The buyer identity binds at bootstrap, and this session
          // bootstrapped anonymously. Drop it so the next send
          // re-bootstraps — picking up whatever token
          // getFirebaseIdToken returns after the visitor signs in —
          // instead of paywalling a signed-in customer until a full
          // page reload.
          this.bootstrapResult = undefined;
          this.cursor = 0;
        }
      }
      subscriber.next({
        type: EventType.RUN_ERROR,
        message,
        code,
      } as BaseEvent);
      closeEventSource(ctx.source);
      ctx.source = undefined;
      subscriber.complete();
    }
  }

  /**
   * Tear down whatever EventSource is currently live on ``ctx`` and
   * open a fresh one against *sessionId* starting after the cursor.  Called
   * for the run's stream and for every reopen of it.  Always clears
   * the stale ``onerror`` first so the old source can't race a
   * ``RUN_ERROR`` into the subscriber after it's been replaced.
   */
  private replaceStreamSource(
    ctx: RunContext,
    sessionId: string,
    translator: Translator,
    subscriber: Subscriber<BaseEvent>,
    input: RunAgentInput,
  ): void {
    closeEventSource(ctx.source);
    const source = openEventStream(this.apiUrl, sessionId, this.cursor || -1, this.agentId);
    ctx.source = source;
    this.attachStreamListeners(
      source, translator, subscriber, input, ctx,
      () => this.replaceStreamSource(ctx, sessionId, translator, subscriber, input),
    );
  }

  /**
   * Try ``sendMessage``; on auth failure re-bootstrap once and retry.
   *
   * Returns the session the message landed in and its event id.  The
   * re-bootstrap mints a new session id; visitor continuity across
   * bootstraps is a deliberate non-feature for v1, so the new session
   * starts empty.
   */
  private async sendWithCookieRecovery(
    boot: BootstrapResult,
    content: string,
  ): Promise<{ sessionId: string; eventId: number }> {
    try {
      const { eventId } = await sendMessage(
        this.apiUrl, boot.sessionId, boot.csrfToken, content, this.agentId,
      );
      return { sessionId: boot.sessionId, eventId };
    } catch (err) {
      if (!(err instanceof SurogatesAuthError)) throw err;
      this.bootstrapResult = undefined;
      this.cursor = 0;
      const fresh = await this.ensureBootstrapped();
      const { eventId } = await sendMessage(
        this.apiUrl, fresh.sessionId, fresh.csrfToken, content, this.agentId,
      );
      return { sessionId: fresh.sessionId, eventId };
    }
  }

  /**
   * Wire up the EventSource to translate and forward every frame,
   * and terminate the observable when the translator flags
   * end-of-turn or a transport failure occurs.
   *
   * After the translator flips end-of-turn we emit ``RUN_FINISHED``
   * immediately, then keep the stream open for ``DRAIN_WINDOW_MS``
   * so trailing frames (late ``memory.update``, the sentinel
   * ``session.done``) advance ``this.cursor`` rather than arriving
   * on the next run.  The ``ctx.terminated`` gate stops additional
   * subscriber emissions during the drain.
   */
  private attachStreamListeners(
    source: EventSource,
    translator: Translator,
    subscriber: Subscriber<BaseEvent>,
    input: RunAgentInput,
    ctx: RunContext,
    reopen: () => void,
  ): void {
    const fail = (message: string, code: string) => {
      if (ctx.terminated) return;
      ctx.terminated = true;
      closeEventSource(source);
      if (ctx.source === source) ctx.source = undefined;
      subscriber.next({ type: EventType.RUN_ERROR, message, code } as BaseEvent);
      subscriber.complete();
    };

    // Reopen from the cursor while the run is live.  The server's
    // ``stream.timeout`` is an orderly close and reopens at once; a
    // transport failure waits a growing delay first, and gives up as
    // RUN_ERROR once the delays run out.
    const reconnect = (afterFailure: boolean) => {
      if (ctx.terminated || ctx.source !== source || ctx.reconnectTimer !== undefined) return;
      closeEventSource(source);
      if (!afterFailure) {
        reopen();
        return;
      }
      const delay = RECONNECT_DELAYS_MS[ctx.failedReconnects];
      if (delay === undefined) {
        fail('SSE connection closed', 'sse_closed');
        return;
      }
      ctx.failedReconnects += 1;
      ctx.reconnectTimer = globalThis.setTimeout(() => {
        ctx.reconnectTimer = undefined;
        if (!ctx.terminated) reopen();
      }, delay);
    };

    const scheduleDrainClose = () => {
      // ``globalThis`` so the SDK works in both browser (returns
      // number) and Node (returns Timeout) without a type cast.
      if (ctx.drainTimer !== undefined) globalThis.clearTimeout(ctx.drainTimer);
      ctx.drainTimer = globalThis.setTimeout(() => {
        // Null the timer ref BEFORE closing the source so a
        // simultaneous ``replaceStreamSource`` (from cookie recovery)
        // doesn't see a stale timer belonging to the old stream.
        ctx.drainTimer = undefined;
        closeEventSource(source);
      }, DRAIN_WINDOW_MS);
    };

    const finish = () => {
      if (ctx.terminated) return;
      ctx.terminated = true;
      subscriber.next({
        type: EventType.RUN_FINISHED,
        threadId: input.threadId,
        runId: input.runId,
      } as BaseEvent);
      // Schedule the drain BEFORE ``subscriber.complete()`` so the
      // teardown invoked by RxJS sees ``ctx.drainTimer`` set and
      // leaves the source alone.  Reverse the order and the teardown
      // runs with ``drainTimer === undefined``, closes the source
      // immediately, and the subsequently-scheduled timer finds
      // nothing to close -- turning the drain into a no-op and
      // breaking the cursor-advancement guarantee this whole dance
      // exists to provide.
      scheduleDrainClose();
      subscriber.complete();
    };

    const handleFrame = (name: string, data: string, lastEventId: string) => {
      if (name === SURG_EVENT.STREAM_TIMEOUT) {
        reconnect(false);
        return;
      }
      const numbered = !UNNUMBERED_FRAMES.has(name);
      const id = numbered ? Number(lastEventId) || 0 : 0;
      // A reopened stream, or the browser's own reconnect, can hand back
      // frames this run has already translated.
      if (id > 0 && id <= this.cursor) return;
      ctx.failedReconnects = 0;
      let parsed: unknown;
      try {
        parsed = JSON.parse(data);
      } catch {
        parsed = data;
      }
      const frame: SurogatesFrame = { id, type: name, data: parsed };
      // Cursor advancement happens BEFORE the termination guard so
      // frames arriving during the drain window still push the
      // cursor past them.  This is the whole point of the drain:
      // absorb trailing server frames so the next run's ``?after=``
      // starts beyond them.
      if (frame.id > this.cursor) this.cursor = frame.id;
      if (ctx.terminated) return;

      const events = translator.translate(frame);
      for (const e of events) subscriber.next(e);
      if (translator.isTurnComplete) finish();
    };

    // Subscribe to every Surogates event type by name.  ``EventSource``
    // default ``onmessage`` only fires for frames without an ``event:``
    // line, but the server always sets one.
    for (const name of SURG_EVENT_NAMES) {
      source.addEventListener(name, (ev: MessageEvent) => {
        handleFrame(name, ev.data, ev.lastEventId);
      });
    }

    // Transport-level failures.  The agent reopens the stream itself
    // rather than leave it to the browser, whose reconnect would ask
    // again for the URL first opened and could keep failing silently.
    // ``ctx.terminated`` stops this after finish() / the driveRun catch
    // have already terminated.
    source.onerror = () => reconnect(true);
  }

  /**
   * Find the last user-authored message body to post.  AG-UI supports
   * multimodal content arrays; this v1 only handles string content
   * (the website channel's ``POST /messages`` endpoint accepts plain
   * text).  Multimodal support lands once the server grows a media
   * upload path.
   */
  private extractUserText(messages: Message[]): string {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (!m || m.role !== 'user') continue;
      const content = (m as { content?: unknown }).content;
      if (typeof content === 'string' && content.trim().length > 0) {
        return content;
      }
    }
    throw new AGUIError(
      'WebsiteAgent: no user message with string content found in input.messages.',
    );
  }
}

/** Per-run mutable state, scoped to the Observable factory closure. */
interface RunContext {
  terminated: boolean;
  source: EventSource | undefined;
  drainTimer: ReturnType<typeof setTimeout> | undefined;
  reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  /** Consecutive failed reopens; a received frame resets it. */
  failedReconnects: number;
}

/**
 * Detach the ``onerror`` listener BEFORE ``close()`` so the old
 * EventSource can't race a terminal error into the subscriber after
 * it's been swapped for a replacement (cookie recovery) or the run
 * has otherwise terminated.  Safe to call on undefined.
 */
function closeEventSource(source: EventSource | undefined): void {
  if (!source) return;
  try {
    source.onerror = null;
  } catch {
    /* Older EventSource implementations disallow null; ignore. */
  }
  source.close();
}
