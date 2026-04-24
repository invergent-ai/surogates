/**
 * Unit tests for the Surogates-frame → AG-UI-event translator.
 *
 * These run without a live server -- they feed synthetic frames that
 * mirror what the ``/v1/website/sessions/{id}/events`` SSE stream emits
 * and assert on the ``BaseEvent`` array the translator produces.
 *
 * The goal is to lock in three invariants:
 *   1. Chunk-style events for streaming text and tool calls (so the
 *      AG-UI client's ``transformChunks`` pipeline expands them into
 *      proper START/CONTENT/END triads).
 *   2. End-of-turn detection based on the ``llm.response`` payload.
 *   3. Platform-specific events (memory, context compact, ...) fall
 *      through to CUSTOM with the original Surogates type in ``name``.
 */

import type { BaseEvent } from '@ag-ui/client';
import { describe, expect, it } from 'vitest';
import { EventType } from '@ag-ui/client';

import { Translator, type SurogatesFrame } from '../src/translator.js';
import { SURG_EVENT } from '../src/constants.js';

function frame(type: string, data: unknown, id = 1): SurogatesFrame {
  return { id, type, data };
}

/**
 * AG-UI's ``BaseEvent`` is a discriminated union; the helper just lets
 * tests read a typed field off whichever variant the translator
 * emitted without leaking ``any`` into the assertions.  Returns
 * ``undefined`` if the field is absent, mirroring optional props.
 */
function field<T = unknown>(ev: BaseEvent, key: string): T | undefined {
  return (ev as unknown as Record<string, T | undefined>)[key];
}


describe('Translator -- text streaming', () => {
  it('emits TEXT_MESSAGE_CHUNK with role=assistant for each llm.delta', () => {
    const t = new Translator();
    const out = t.translate(frame(SURG_EVENT.LLM_DELTA, { delta: 'hello' }, 5));
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      type: EventType.TEXT_MESSAGE_CHUNK,
      role: 'assistant',
      delta: 'hello',
    });
  });

  it('reuses the same messageId across consecutive deltas', () => {
    const t = new Translator();
    const a = t.translate(frame(SURG_EVENT.LLM_DELTA, { delta: 'he' }, 1));
    const b = t.translate(frame(SURG_EVENT.LLM_DELTA, { delta: 'llo' }, 2));
    // Same running assistant turn → same messageId so the AG-UI
    // transformChunks pipeline stitches them into one message.
    expect(field<string>(a[0]!, 'messageId')).toBe(field<string>(b[0]!, 'messageId'));
  });

  it('drops empty deltas', () => {
    const t = new Translator();
    expect(t.translate(frame(SURG_EVENT.LLM_DELTA, { delta: '' }))).toEqual([]);
    expect(t.translate(frame(SURG_EVENT.LLM_DELTA, {}))).toEqual([]);
  });

  it('resets messageId after llm.response so the next turn starts fresh', () => {
    const t = new Translator();
    const a = t.translate(frame(SURG_EVENT.LLM_DELTA, { delta: 'turn 1' }, 1));
    t.translate(frame(SURG_EVENT.LLM_RESPONSE, { content: 'turn 1', finish_reason: 'stop' }, 2));
    // Second turn wouldn't normally happen inside one run, but the
    // translator must not leak messageId across turn boundaries.
    const b = t.translate(frame(SURG_EVENT.LLM_DELTA, { delta: 'turn 2' }, 3));
    expect(field<string>(a[0]!, 'messageId')).not.toBe(field<string>(b[0]!, 'messageId'));
  });
});


describe('Translator -- end-of-turn detection', () => {
  it('flips isTurnComplete on llm.response with finish_reason=stop', () => {
    const t = new Translator();
    t.translate(frame(SURG_EVENT.LLM_DELTA, { delta: 'hi' }));
    expect(t.isTurnComplete).toBe(false);
    t.translate(frame(SURG_EVENT.LLM_RESPONSE, { content: 'hi', finish_reason: 'stop' }));
    expect(t.isTurnComplete).toBe(true);
  });

  it('flips isTurnComplete when no tool_calls are present', () => {
    // Some server paths omit finish_reason entirely; the fallback is
    // to treat "no pending tool calls AND empty tool_calls array" as
    // end-of-turn.  This is the most common case in practice.
    const t = new Translator();
    t.translate(frame(SURG_EVENT.LLM_RESPONSE, { content: 'done', tool_calls: [] }));
    expect(t.isTurnComplete).toBe(true);
  });

  it('does NOT flip isTurnComplete when tool_calls are pending', () => {
    const t = new Translator();
    t.translate(
      frame(SURG_EVENT.TOOL_CALL, {
        tool_call_id: 'tc-1',
        name: 'web_search',
        arguments: { q: 'x' },
      }),
    );
    t.translate(frame(SURG_EVENT.LLM_RESPONSE, { content: '...', finish_reason: 'tool_calls' }));
    expect(t.isTurnComplete).toBe(false);
  });

  it('does NOT flip on finish_reason=tool_calls even with empty tool_calls array', () => {
    // Some provider adapters stream the "I'll use a tool" sentinel
    // ahead of the actual tool definitions, arriving as an
    // ``llm.response`` with ``finish_reason: 'tool_calls'`` and an
    // empty array.  Prior to the fix, ``toolCalls.length === 0``
    // matched and flipped us done before the tool call events even
    // arrived.  Regression guard.
    const t = new Translator();
    t.translate(
      frame(SURG_EVENT.LLM_RESPONSE, { content: null, tool_calls: [], finish_reason: 'tool_calls' }),
    );
    expect(t.isTurnComplete).toBe(false);
  });

  it('does NOT flip on Anthropic-style stop_reason=tool_use', () => {
    // Anthropic's native ``stop_reason`` uses ``tool_use`` (not
    // ``tool_calls``).  We treat both as "continuation" so the turn
    // stays open until the tool.result + next llm.response arrive.
    const t = new Translator();
    t.translate(
      frame(SURG_EVENT.LLM_RESPONSE, { content: null, tool_calls: [], stop_reason: 'tool_use' }),
    );
    expect(t.isTurnComplete).toBe(false);
  });

  it('flips on session.done sentinel', () => {
    const t = new Translator();
    t.translate(frame(SURG_EVENT.SESSION_DONE, {}));
    expect(t.isTurnComplete).toBe(true);
  });

  it('flips + emits RUN_ERROR on session.fail', () => {
    const t = new Translator();
    const out = t.translate(frame(SURG_EVENT.SESSION_FAIL, { error: 'harness crashed' }));
    expect(t.isTurnComplete).toBe(true);
    expect(out[0]).toMatchObject({
      type: EventType.RUN_ERROR,
      message: 'harness crashed',
    });
  });
});


describe('Translator -- tool calls', () => {
  it('emits TOOL_CALL_CHUNK with full args on tool.call', () => {
    const t = new Translator();
    const out = t.translate(
      frame(SURG_EVENT.TOOL_CALL, {
        tool_call_id: 'tc-abc',
        name: 'web_search',
        arguments: { query: 'pythagoras' },
      }),
    );
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      type: EventType.TOOL_CALL_CHUNK,
      toolCallId: 'tc-abc',
      toolCallName: 'web_search',
    });
    // Arguments get JSON-stringified for the wire.
    expect(field<string>(out[0]!, 'delta')).toBe('{"query":"pythagoras"}');
  });

  it('emits TOOL_CALL_RESULT on tool.result', () => {
    const t = new Translator();
    const out = t.translate(
      frame(SURG_EVENT.TOOL_RESULT, {
        tool_call_id: 'tc-abc',
        name: 'web_search',
        content: 'answer...',
      }),
    );
    expect(out[0]).toMatchObject({
      type: EventType.TOOL_CALL_RESULT,
      toolCallId: 'tc-abc',
      content: 'answer...',
    });
  });

  it('decrements pending-tool-calls when a result arrives', () => {
    const t = new Translator();
    t.translate(
      frame(SURG_EVENT.TOOL_CALL, {
        tool_call_id: 'tc-abc',
        name: 'web_search',
        arguments: {},
      }),
    );
    t.translate(
      frame(SURG_EVENT.TOOL_RESULT, {
        tool_call_id: 'tc-abc',
        name: 'web_search',
        content: 'ok',
      }),
    );
    // Now a final llm.response should flip the turn.
    t.translate(frame(SURG_EVENT.LLM_RESPONSE, { content: 'done', tool_calls: [] }));
    expect(t.isTurnComplete).toBe(true);
  });
});


describe('Translator -- reasoning', () => {
  it('opens a reasoning stream on first llm.thinking', () => {
    const t = new Translator();
    const out = t.translate(frame(SURG_EVENT.LLM_THINKING, { delta: 'let me think' }));
    // First thinking chunk opens the reasoning + reasoning-message
    // pair and then emits the content delta -- three events together.
    const types = out.map((e) => e.type);
    expect(types).toContain(EventType.REASONING_START);
    expect(types).toContain(EventType.REASONING_MESSAGE_START);
    expect(types).toContain(EventType.REASONING_MESSAGE_CONTENT);
  });

  it('does not re-open the reasoning stream for subsequent chunks', () => {
    const t = new Translator();
    t.translate(frame(SURG_EVENT.LLM_THINKING, { delta: 'a' }));
    const out = t.translate(frame(SURG_EVENT.LLM_THINKING, { delta: 'b' }));
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      type: EventType.REASONING_MESSAGE_CONTENT,
      delta: 'b',
    });
  });
});


describe('Translator -- policy + experts + fallthrough', () => {
  it('maps policy.denied to CUSTOM (tool + reason)', () => {
    const t = new Translator();
    const out = t.translate(
      frame(SURG_EVENT.POLICY_DENIED, { tool: 'terminal', reason: 'not in allow-list' }),
    );
    expect(out[0]).toMatchObject({
      type: EventType.CUSTOM,
      name: SURG_EVENT.POLICY_DENIED,
      value: { tool: 'terminal', reason: 'not in allow-list' },
    });
  });

  it('maps expert.delegation/result to STEP_STARTED/STEP_FINISHED', () => {
    const t = new Translator();
    const start = t.translate(
      frame(SURG_EVENT.EXPERT_DELEGATION, { expert_name: 'sql_writer' }),
    );
    const end = t.translate(
      frame(SURG_EVENT.EXPERT_RESULT, { expert_name: 'sql_writer', result: {} }),
    );
    expect(start[0]).toMatchObject({
      type: EventType.STEP_STARTED,
      stepName: 'expert:sql_writer',
    });
    expect(end[0]).toMatchObject({
      type: EventType.STEP_FINISHED,
      stepName: 'expert:sql_writer',
    });
  });

  it('forwards memory.update as CUSTOM without changing shape', () => {
    const t = new Translator();
    const out = t.translate(
      frame(SURG_EVENT.MEMORY_UPDATE, { action: 'add', content: 'user likes tea' }),
    );
    expect(out[0]).toMatchObject({
      type: EventType.CUSTOM,
      name: SURG_EVENT.MEMORY_UPDATE,
      value: { action: 'add', content: 'user likes tea' },
    });
  });

  it('drops internal-orchestration events', () => {
    const t = new Translator();
    for (const type of [
      SURG_EVENT.USER_MESSAGE,
      SURG_EVENT.LLM_REQUEST,
      SURG_EVENT.SESSION_START,
      SURG_EVENT.SANDBOX_PROVISION,
      SURG_EVENT.POLICY_ALLOWED,
    ]) {
      expect(t.translate(frame(type, {}))).toEqual([]);
    }
  });

  it('falls through unknown event types to CUSTOM', () => {
    const t = new Translator();
    const out = t.translate(frame('some.future.event', { foo: 1 }));
    expect(out[0]).toMatchObject({
      type: EventType.CUSTOM,
      name: 'some.future.event',
      value: { foo: 1 },
    });
  });
});
