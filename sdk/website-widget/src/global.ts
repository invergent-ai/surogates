/**
 * IIFE entry point for the ``<script>``-tag / CDN build.
 *
 * The npm entry (``src/index.ts``) stays headless so AG-UI consumers
 * who only want the client keep their lean, Preact-free bundle.  This
 * module is the IIFE-only superset: it re-exports the entire headless
 * surface **plus** the self-mounting ``mount()`` UI, so a single
 * ``<script>`` tag puts both ``SurogatesWidget.WebsiteAgent`` and
 * ``SurogatesWidget.mount`` on ``window``.
 */
export * from './index.js';
export { mount, mountWithPairing } from './ui/mount.js';
export type { MountConfig, WidgetHandle, WidgetAppearance } from './ui/types.js';
