/**
 * Public entry point of ``@invergent-ai/website-widget``.
 *
 * Exports the :class:`WebsiteAgent` every consumer needs, plus the
 * constructor-config type, the typed error taxonomy, and the low-level
 * event translator for advanced consumers that want to pre-process
 * Surogates frames without the full AG-UI pipeline.
 *
 * The ``@ag-ui/client`` re-exports below let a script-tag / IIFE
 * consumer pull every AG-UI primitive off ``window.SurogatesWidget``
 * without a separate CDN include -- the tsup IIFE build bundles
 * AG-UI, so exposing its surface here is free.  npm consumers can
 * still import from ``@ag-ui/client`` directly; both paths resolve to
 * the same objects.
 */

export { WebsiteAgent } from './agent.js';
export type { WebsiteAgentConfig } from './agent.js';

export { Translator } from './translator.js';
export type { SurogatesFrame } from './translator.js';

export {
  SurogatesError,
  SurogatesAuthError,
  SurogatesNetworkError,
  SurogatesProtocolError,
  SurogatesRateLimitError,
} from './errors.js';

export { PROTOCOL_VERSION, SDK_VERSION, SURG_EVENT } from './constants.js';

// Conveniences re-exported from AG-UI so an IIFE consumer has access
// to the full ``EventType`` enum and ``AbstractAgent`` base class
// without a second ``<script>`` tag.  Both come from
// ``@ag-ui/client``, which re-exports ``@ag-ui/core`` entirely -- so
// consumers only ever need one peer dependency.
export { AbstractAgent, EventType } from '@ag-ui/client';
