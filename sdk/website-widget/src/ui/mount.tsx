/**
 * ``mount()`` — the drop-in entry point.
 *
 * A single call stands up a complete chat widget: it creates a host
 * element, isolates the UI in a Shadow DOM (so neither the host page's
 * CSS nor the widget's styles can bleed across), instantiates the
 * headless :class:`WebsiteAgent`, and renders the Preact UI into the
 * shadow root.  Returns an imperative :type:`WidgetHandle` for hosts
 * that want to open/close/destroy the widget programmatically.
 *
 * This is what the ``<script>``-tag embed and the Studio live preview
 * both call.  Everything cosmetic is optional; ``apiUrl`` and
 * ``publishableKey`` are the only required fields.
 */
import { render } from 'preact';

import { WebsiteAgent } from '../agent.js';
import { WIDGET_STYLES } from './styles.js';
import type { MountConfig, WidgetHandle } from './types.js';
import { Widget } from './widget.js';

export type { MountConfig, WidgetHandle, WidgetAppearance } from './types.js';

const DEFAULT_ACCENT = '#4f46e5';

/**
 * Pick a readable foreground (near-black vs white) for text/icons that
 * sit on the accent colour, using an sRGB luminance approximation.  A
 * pale accent gets dark text; a saturated one gets white.
 */
function accentForeground(hex: string): string {
  const v = hex.replace('#', '');
  const full = v.length === 3 ? v.split('').map((c) => c + c).join('') : v;
  if (full.length !== 6 || /[^0-9a-f]/i.test(full)) return '#ffffff';
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.62 ? '#0f172a' : '#ffffff';
}

export function mount(config: MountConfig): WidgetHandle {
  if (!config || !config.apiUrl || !config.publishableKey) {
    throw new Error(
      'SurogatesWidget.mount: "apiUrl" and "publishableKey" are required.',
    );
  }

  const accent = config.accentColor ?? DEFAULT_ACCENT;
  let destroyed = false;

  // Host element + Shadow DOM: total style isolation in both directions.
  const host = document.createElement('div');
  host.setAttribute('data-surogates-widget', '');
  const shadow = host.attachShadow({ mode: 'open' });

  const style = document.createElement('style');
  style.textContent = WIDGET_STYLES;
  shadow.appendChild(style);

  const root = document.createElement('div');
  root.className = 'surg-root';
  shadow.appendChild(root);

  // Accent drives CSS custom properties on the root, so it can be re-applied
  // on update() without re-rendering anything else.
  const applyAccent = (accentColor?: string) => {
    const a = accentColor ?? DEFAULT_ACCENT;
    root.style.setProperty('--surg-accent', a);
    root.style.setProperty('--surg-accent-fg', accentForeground(a));
  };
  applyAccent(accent);

  // Attach to the page. Drop-in embeds are often placed in <head>, where
  // document.body doesn't exist yet — defer the append until the DOM is ready.
  const attach = () => (config.target ?? document.body)?.appendChild(host);
  if (config.target || document.body) {
    attach();
  } else {
    document.addEventListener(
      "DOMContentLoaded",
      () => {
        if (!destroyed) attach();
      },
      { once: true },
    );
  }

  const agent = new WebsiteAgent(config);

  let setOpen: ((open: boolean) => void) | undefined;
  let isOpen = config.inline || !!config.openByDefault;

  // Stable callbacks so re-rendering on update() doesn't re-fire the
  // component's open-control / open-change effects.
  const registerOpenControl = (fn: (open: boolean) => void) => {
    setOpen = fn;
  };
  const onOpenChange = (o: boolean) => {
    isOpen = o;
  };

  // Current config is mutable so update() can re-render appearance without
  // tearing down the agent (no re-bootstrap, no lost conversation).
  let current: MountConfig = config;
  const renderWidget = () => {
    render(
      <Widget
        agent={agent}
        config={current}
        registerOpenControl={registerOpenControl}
        onOpenChange={onOpenChange}
      />,
      root,
    );
  };
  renderWidget();

  // Best-effort session close when the visitor navigates away.
  const endOnUnload = () => {
    void agent.end();
  };
  window.addEventListener('beforeunload', endOnUnload);

  return {
    agent,
    open: () => setOpen?.(true),
    close: () => setOpen?.(false),
    toggle: () => setOpen?.(!isOpen),
    update: (partial) => {
      if (destroyed) return;
      current = { ...current, ...partial };
      if ('accentColor' in partial) applyAccent(current.accentColor);
      renderWidget();
    },
    destroy: () => {
      if (destroyed) return;
      destroyed = true;
      window.removeEventListener('beforeunload', endOnUnload);
      void agent.end();
      render(null, root);
      host.remove();
    },
  };
}
