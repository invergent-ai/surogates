/**
 * Wire-protocol and SDK-version constants.
 *
 * ``PROTOCOL_VERSION`` is the integer the SDK sends to the server so
 * future breaking protocol changes can be negotiated or hard-failed at
 * bootstrap instead of silently corrupting running embeds.
 *
 * ``SDK_VERSION`` travels in an ``X-Surogates-Widget-Version`` header on
 * every request so server logs can correlate a buggy client build with
 * its error surface.  Kept in sync with ``package.json`` manually; the
 * build pipeline can replace it at bundle time in a later phase.
 */

export const PROTOCOL_VERSION = 1;

export const SDK_VERSION = '0.1.0';

// Header names we read/write.  Lifted to constants so tests, the client,
// and the protocol layer can't drift from each other.
export const CSRF_HEADER = 'X-CSRF-Token';
export const VERSION_HEADER = 'X-Surogates-Widget-Version';

// URL segments of the website-channel API, relative to ``apiUrl``.
export const PATH_BOOTSTRAP = '/v1/website/sessions';
export const PATH_MESSAGES = (sessionId: string) =>
  `/v1/website/sessions/${sessionId}/messages`;
export const PATH_EVENTS = (sessionId: string, after: number) =>
  `/v1/website/sessions/${sessionId}/events?after=${after}`;
export const PATH_END = (sessionId: string) =>
  `/v1/website/sessions/${sessionId}/end`;

// Surogates native event type strings the SSE stream carries.  Source
// of truth: ``surogates/session/events.py``.  Duplicated here so the
// translator can switch on them without a network of string literals
// scattered through the code.
export const SURG_EVENT = {
  USER_MESSAGE: 'user.message',
  LLM_REQUEST: 'llm.request',
  LLM_RESPONSE: 'llm.response',
  LLM_THINKING: 'llm.thinking',
  LLM_DELTA: 'llm.delta',
  TOOL_CALL: 'tool.call',
  TOOL_RESULT: 'tool.result',
  SANDBOX_PROVISION: 'sandbox.provision',
  SANDBOX_EXECUTE: 'sandbox.execute',
  SANDBOX_RESULT: 'sandbox.result',
  SANDBOX_DESTROY: 'sandbox.destroy',
  SESSION_START: 'session.start',
  SESSION_PAUSE: 'session.pause',
  SESSION_RESUME: 'session.resume',
  SESSION_COMPLETE: 'session.complete',
  SESSION_FAIL: 'session.fail',
  SESSION_DONE: 'session.done',
  CONTEXT_COMPACT: 'context.compact',
  MEMORY_UPDATE: 'memory.update',
  EXPERT_DELEGATION: 'expert.delegation',
  EXPERT_RESULT: 'expert.result',
  EXPERT_FAILURE: 'expert.failure',
  POLICY_DENIED: 'policy.denied',
  POLICY_ALLOWED: 'policy.allowed',
  HARNESS_CRASH: 'harness.crash',
} as const;
