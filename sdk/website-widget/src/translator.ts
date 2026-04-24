/**
 * Pure event translation: Surogates wire events → AG-UI ``BaseEvent``\ s.
 *
 * The SSE stream delivered by ``/v1/website/sessions/{id}/events`` carries
 * event types defined in ``surogates/session/events.py``
 * (``user.message``, ``llm.delta``, ``llm.response``, ``tool.call``, ...).
 * AG-UI consumers expect a different, standardised vocabulary
 * (``TEXT_MESSAGE_START`` / ``CONTENT`` / ``END`` triads, ``TOOL_CALL_*``,
 * ``RUN_STARTED`` / ``RUN_FINISHED``, ``STATE_SNAPSHOT``, etc.).  This
 * module is the contract.
 *
 * Design notes
 * ------------
 * * **Pure and state-carried.**  ``Translator`` is a class only because
 *   a few decisions need a tiny bit of memory (have we emitted the
 *   running ``TextMessageStart`` yet?  how many tool calls are
 *   outstanding?  did the last ``llm.response`` signal end-of-turn?).
 *   It does not touch the network or any globals; every instance is a
 *   single run's translation state and discardable after
 *   ``RUN_FINISHED``.
 *
 * * **Chunks over triads.**  AG-UI provides
 *   ``TEXT_MESSAGE_CHUNK`` / ``TOOL_CALL_CHUNK`` convenience events
 *   that expand to the START / CONTENT / END triad automatically via
 *   the client-side ``transformChunks`` pipeline.  Surogates already
 *   streams in chunks (``llm.delta``, full-payload ``tool.call``), so
 *   emitting chunk events keeps the translator short and matches the
 *   data we actually have.
 *
 * * **Unknown → CUSTOM.**  Platform-specific events we cannot map to
 *   first-class AG-UI events (memory updates, saga step transitions,
 *   session resets, context compaction) are emitted as AG-UI ``CUSTOM``
 *   events with ``name`` set to the Surogates event type and ``value``
 *   set to the raw payload.  Consumers that care can match on
 *   ``name``; consumers that don't simply ignore CUSTOM.
 *
 * * **End-of-turn heuristic.**  Our server does not emit a
 *   ``turn.complete`` event.  Per-turn completion is inferred: an
 *   ``llm.response`` whose payload indicates the model is done (no
 *   pending tool calls, ``finish_reason`` of ``stop`` / ``end_turn``)
 *   flips ``isTurnComplete`` to ``true``.  The caller emits
 *   ``RUN_FINISHED`` and closes the stream.  A ``session.done`` SSE
 *   sentinel also completes the turn.
 */

import type { BaseEvent } from '@ag-ui/client';
import { EventType } from '@ag-ui/client';

import { SURG_EVENT } from './constants.js';

/** Minimal shape of a single SSE frame we read from the server. */
export interface SurogatesFrame {
  /** Monotonic server-side event id (cursor for reconnect). */
  id: number;
  /** Surogates event type (``user.message``, ``llm.delta``, etc.). */
  type: string;
  /** Parsed JSON payload from the ``data:`` line. */
  data: unknown;
}

/** Payload keys we pull off ``llm.response`` events to detect end-of-turn. */
interface LlmResponsePayload {
  content?: string;
  tool_calls?: unknown[];
  finish_reason?: string;
  stop_reason?: string;
  message_id?: string;
  id?: string;
}

/** Payload keys for ``llm.delta`` events. */
interface LlmDeltaPayload {
  delta?: string;
  content?: string;
  message_id?: string;
}

/** Payload keys for ``tool.call`` events. */
interface ToolCallPayload {
  tool_call_id?: string;
  name?: string;
  arguments?: unknown;
}

/** Payload keys for ``tool.result`` events. */
interface ToolResultPayload {
  tool_call_id?: string;
  name?: string;
  content?: string;
}

/** Payload keys for ``llm.thinking`` events. */
interface LlmThinkingPayload {
  delta?: string;
  content?: string;
  message_id?: string;
}

/** Payload keys for ``policy.denied`` events. */
interface PolicyDeniedPayload {
  tool?: string;
  reason?: string;
}

/** Payload keys for expert delegation events. */
interface ExpertDelegationPayload {
  expert_name?: string;
  task?: string;
}

interface ExpertResultPayload {
  expert_name?: string;
  result?: unknown;
}

const END_OF_TURN_FINISH_REASONS = new Set(['stop', 'end_turn', 'length']);

/**
 * Finish reasons that explicitly signal "more work is coming" --
 * currently just OpenAI-style ``tool_calls``.  Treated as an
 * affirmative negation of end-of-turn even if the provider happens
 * to emit an empty ``tool_calls`` array alongside it (some adapters
 * do).  Kept as a set so future provider quirks can be added without
 * touching the detection logic.
 */
const CONTINUATION_FINISH_REASONS = new Set(['tool_calls', 'function_call', 'tool_use']);

/**
 * Stateful translator for one run.
 *
 * The caller is expected to build a fresh instance per ``runAgent`` turn
 * and discard it after the run finishes; every method is idempotent and
 * thread-safe only in the single-SSE-consumer sense (JavaScript events
 * are single-threaded).
 */
export class Translator {
  /**
   * Running message id for the current streaming assistant message.
   * Re-used across ``llm.delta`` frames so every chunk targets the
   * same AG-UI message -- that's what makes the client-side
   * ``transformChunks`` stitch them into one logical message.
   */
  private currentMessageId: string | undefined;

  /**
   * Running message id for the current reasoning stream, if any.
   * Kept separate from ``currentMessageId`` so reasoning deltas never
   * accidentally merge into the assistant content stream.
   */
  private currentReasoningId: string | undefined;

  /**
   * Tool calls that have been announced (``TOOL_CALL_CHUNK`` emitted)
   * but have not yet received a matching ``tool.result``.  Non-empty
   * means the turn is mid-execution even if an ``llm.response`` looks
   * final.
   */
  private pendingToolCalls = new Set<string>();

  /** Set once we've seen an end-of-turn signal. */
  private _turnComplete = false;

  get isTurnComplete(): boolean {
    return this._turnComplete;
  }

  /**
   * Translate one SSE frame into zero or more AG-UI events.
   *
   * Returns an empty array when the frame carries no user-visible
   * information (internal bookkeeping events like ``harness.wake``).
   * Returns multiple events when a single Surogates event corresponds
   * to a chunk triad that the AG-UI client can't reconstruct on its
   * own -- though we try to use chunk events to keep this to one
   * output event per input in the common path.
   */
  translate(frame: SurogatesFrame): BaseEvent[] {
    switch (frame.type) {
      case SURG_EVENT.LLM_DELTA:
        return this.handleLlmDelta(frame);

      case SURG_EVENT.LLM_RESPONSE:
        return this.handleLlmResponse(frame);

      case SURG_EVENT.LLM_THINKING:
        return this.handleLlmThinking(frame);

      case SURG_EVENT.TOOL_CALL:
        return this.handleToolCall(frame);

      case SURG_EVENT.TOOL_RESULT:
        return this.handleToolResult(frame);

      case SURG_EVENT.POLICY_DENIED:
        return this.handlePolicyDenied(frame);

      case SURG_EVENT.EXPERT_DELEGATION:
        return this.handleExpertDelegation(frame);

      case SURG_EVENT.EXPERT_RESULT:
        return this.handleExpertResult(frame);

      case SURG_EVENT.EXPERT_FAILURE:
        return this.asCustom(frame);

      case SURG_EVENT.SESSION_FAIL:
      case SURG_EVENT.HARNESS_CRASH:
        return this.handleTerminalFailure(frame);

      case SURG_EVENT.SESSION_DONE:
      case SURG_EVENT.SESSION_COMPLETE:
        this._turnComplete = true;
        return [];

      // Frames we do not surface to AG-UI consumers: internal
      // orchestration (``user.message`` is already in the consumer's
      // own ``agent.messages``; ``llm.request``, ``session.start``,
      // ``sandbox.*``, ``harness.wake`` are implementation details).
      case SURG_EVENT.USER_MESSAGE:
      case SURG_EVENT.LLM_REQUEST:
      case SURG_EVENT.SESSION_START:
      case SURG_EVENT.SESSION_PAUSE:
      case SURG_EVENT.SESSION_RESUME:
      case SURG_EVENT.SANDBOX_PROVISION:
      case SURG_EVENT.SANDBOX_EXECUTE:
      case SURG_EVENT.SANDBOX_RESULT:
      case SURG_EVENT.SANDBOX_DESTROY:
      case SURG_EVENT.POLICY_ALLOWED:
        return [];

      // Everything else (``memory.update``, ``context.compact``,
      // saga events, future server additions) is forwarded as CUSTOM
      // so advanced consumers that want that visibility can still
      // subscribe -- without us pre-committing to a particular AG-UI
      // shape for a Surogates-specific event that might evolve.
      default:
        return this.asCustom(frame);
    }
  }

  // ------------------------------------------------------------------
  // Individual handlers
  // ------------------------------------------------------------------

  private handleLlmDelta(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as LlmDeltaPayload;
    const delta = payload.delta ?? payload.content ?? '';
    if (!delta) return [];

    // Chunks must carry a stable messageId on the FIRST chunk so
    // transformChunks can open the TextMessageStart; later chunks can
    // reuse the same id.  Surogates streams multiple assistant turns
    // in one session, so we mint a new id whenever we've closed one
    // (set back to undefined in handleLlmResponse).
    const messageId = this.currentMessageId ?? payload.message_id ?? this.mintMessageId(frame.id);
    this.currentMessageId = messageId;

    return [
      {
        type: EventType.TEXT_MESSAGE_CHUNK,
        messageId,
        role: 'assistant',
        delta,
      } as BaseEvent,
    ];
  }

  private handleLlmResponse(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as LlmResponsePayload;
    const events: BaseEvent[] = [];

    // Close any open assistant message by resetting the current id.
    // transformChunks will emit TEXT_MESSAGE_END when the next chunk
    // targets a new messageId (or the stream completes).  If no delta
    // was seen but the response carries content, emit a single chunk
    // so downstream consumers get something to render.
    if (!this.currentMessageId && payload.content) {
      const messageId = payload.message_id ?? payload.id ?? this.mintMessageId(frame.id);
      events.push({
        type: EventType.TEXT_MESSAGE_CHUNK,
        messageId,
        role: 'assistant',
        delta: payload.content,
      } as BaseEvent);
    }
    this.currentMessageId = undefined;

    // End-of-turn detection is the conjunction of three guards:
    //   1. ``finish_reason`` does NOT say "more work is coming".  Some
    //      provider adapters emit a bare ``llm.response`` with
    //      ``finish_reason: 'tool_calls'`` and an empty ``tool_calls``
    //      array when they stream the "I'll use a tool now" sentinel
    //      separately from the tool definitions.  Treating this as
    //      end-of-turn would flip us done before the tool calls even
    //      arrive.  The positive check on ``CONTINUATION_FINISH_REASONS``
    //      rules that out first.
    //   2. The ``tool_calls`` array is empty.  If the response carries
    //      any tool calls at all, more frames follow.
    //   3. No tool calls that were already announced are still pending
    //      a ``tool.result``.  Belt-and-suspenders: if the server sends
    //      ``llm.response`` before all tool results drain, we keep the
    //      stream open until they do.
    const toolCalls = Array.isArray(payload.tool_calls) ? payload.tool_calls : [];
    const finish = payload.finish_reason ?? payload.stop_reason ?? '';
    if (CONTINUATION_FINISH_REASONS.has(finish)) {
      return events;
    }
    const modelDone =
      toolCalls.length === 0 && (finish === '' || END_OF_TURN_FINISH_REASONS.has(finish));
    if (modelDone && this.pendingToolCalls.size === 0) {
      this._turnComplete = true;
    }

    return events;
  }

  private handleLlmThinking(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as LlmThinkingPayload;
    const delta = payload.delta ?? payload.content ?? '';
    if (!delta) return [];

    const messageId = this.currentReasoningId ?? payload.message_id ?? this.mintMessageId(frame.id);
    const isNew = this.currentReasoningId !== messageId;
    this.currentReasoningId = messageId;

    const events: BaseEvent[] = [];
    if (isNew) {
      events.push({
        type: EventType.REASONING_START,
        messageId,
      } as BaseEvent);
      events.push({
        type: EventType.REASONING_MESSAGE_START,
        messageId,
        role: 'reasoning',
      } as BaseEvent);
    }
    events.push({
      type: EventType.REASONING_MESSAGE_CONTENT,
      messageId,
      delta,
    } as BaseEvent);
    return events;
  }

  private handleToolCall(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as ToolCallPayload;
    const toolCallId = payload.tool_call_id ?? `tc-${frame.id}`;
    const toolCallName = payload.name ?? 'unknown_tool';

    // Close any running reasoning/text message first so the tool call
    // slots in cleanly between them.  We don't emit explicit END
    // events because transformChunks handles that when messageId
    // switches to undefined; we just clear our running ids.
    this.currentMessageId = undefined;
    this.currentReasoningId = undefined;

    this.pendingToolCalls.add(toolCallId);

    // Surogates gives us the FULL arguments in one frame; emit as a
    // single TOOL_CALL_CHUNK which the AG-UI client expands into
    // START + ARGS + END.
    const args =
      typeof payload.arguments === 'string'
        ? payload.arguments
        : JSON.stringify(payload.arguments ?? {});

    return [
      {
        type: EventType.TOOL_CALL_CHUNK,
        toolCallId,
        toolCallName,
        delta: args,
      } as BaseEvent,
    ];
  }

  private handleToolResult(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as ToolResultPayload;
    const toolCallId = payload.tool_call_id ?? `tc-${frame.id}`;
    const content = payload.content ?? '';

    this.pendingToolCalls.delete(toolCallId);

    // AG-UI's TOOL_CALL_RESULT needs a ``messageId`` on the tool
    // message it lands on in the assistant history; we synthesise a
    // per-call id so the AG-UI message reducer can link result to
    // call.  Consumers that need stable message ids across reconnects
    // can pull them from ``frame.id``.
    return [
      {
        type: EventType.TOOL_CALL_RESULT,
        messageId: `tr-${frame.id}`,
        toolCallId,
        content,
        role: 'tool',
      } as BaseEvent,
    ];
  }

  private handlePolicyDenied(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as PolicyDeniedPayload;
    // Policy denials are informational -- the LLM still continues and
    // the next ``llm.response`` will explain the refusal in natural
    // language.  Surface as CUSTOM so UIs can render a "blocked" badge
    // without hijacking the run lifecycle.
    return [
      {
        type: EventType.CUSTOM,
        name: SURG_EVENT.POLICY_DENIED,
        value: { tool: payload.tool, reason: payload.reason },
      } as BaseEvent,
    ];
  }

  private handleExpertDelegation(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as ExpertDelegationPayload;
    const name = payload.expert_name ?? 'expert';
    // Expert delegation is a natural fit for STEP_STARTED -- it's a
    // bounded sub-task the host can render as a progress row.  Kept
    // paired with STEP_FINISHED in ``handleExpertResult``.
    return [
      {
        type: EventType.STEP_STARTED,
        stepName: `expert:${name}`,
      } as BaseEvent,
    ];
  }

  private handleExpertResult(frame: SurogatesFrame): BaseEvent[] {
    const payload = (frame.data ?? {}) as ExpertResultPayload;
    const name = payload.expert_name ?? 'expert';
    return [
      {
        type: EventType.STEP_FINISHED,
        stepName: `expert:${name}`,
      } as BaseEvent,
    ];
  }

  private handleTerminalFailure(frame: SurogatesFrame): BaseEvent[] {
    this._turnComplete = true;
    const payload = (frame.data ?? {}) as { error?: string; message?: string };
    const message = payload.error ?? payload.message ?? `${frame.type} fired`;
    return [
      {
        type: EventType.RUN_ERROR,
        message,
        code: frame.type,
      } as BaseEvent,
    ];
  }

  private asCustom(frame: SurogatesFrame): BaseEvent[] {
    return [
      {
        type: EventType.CUSTOM,
        name: frame.type,
        value: frame.data,
      } as BaseEvent,
    ];
  }

  /**
   * Generate a stable message id derived from the triggering frame's
   * event id.  The event id is globally unique per session, so using
   * it as a suffix guarantees uniqueness without pulling in ``uuid``.
   */
  private mintMessageId(frameId: number): string {
    return `msg-${frameId}`;
  }
}
