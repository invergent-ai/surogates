/**
 * Public types for the self-mounting widget UI.  Kept in their own
 * module so ``widget.tsx`` and ``mount.tsx`` share them without an
 * import cycle, and so consumers importing from
 * ``@invergent/website-widget/ui`` get a clean surface.
 */
import type { WebsiteAgent, WebsiteAgentConfig } from '../agent.js';

/** Purely cosmetic, client-side options.  None of these reach the server. */
export interface WidgetAppearance {
  /** Header label.  Defaults to the bootstrapped ``agentName``. */
  title?: string;
  /** Small line under the title (e.g. "Typically replies in a minute"). */
  subtitle?: string;
  /**
   * Logo/avatar shown in the header and launcher.  Any browser-loadable
   * image URL — a public ``https://`` URL or a ``data:`` URI (so a host
   * can inline a small logo without hosting it).  Falls back to a chat
   * glyph when unset.
   */
  logoUrl?: string;
  /** Brand colour for the launcher, header, user bubbles, and send button. */
  accentColor?: string;
  /** Optional greeting shown as an assistant bubble before the first turn. */
  welcomeMessage?: string;
  /** Which corner the floating launcher docks to.  Default ``bottom-right``. */
  position?: 'bottom-right' | 'bottom-left';
}

/** Argument to :func:`mount`. */
export interface MountConfig extends WebsiteAgentConfig, WidgetAppearance {
  /** Where to attach the widget host element.  Defaults to ``document.body``. */
  target?: HTMLElement;
  /**
   * Render the chat panel inline (filling ``target``) with no floating
   * launcher.  Used by hosts that embed the chat in their own layout —
   * e.g. the Studio live preview.
   */
  inline?: boolean;
  /** Open the panel immediately on mount (ignored when ``inline``). */
  openByDefault?: boolean;
}

/** Imperative handle returned by :func:`mount`. */
export interface WidgetHandle {
  /** Open the chat panel. */
  open(): void;
  /** Close the chat panel (no-op in ``inline`` mode). */
  close(): void;
  /** Toggle the chat panel. */
  toggle(): void;
  /**
   * Update appearance (title/subtitle/logo/accent/welcome/position) in place,
   * without tearing down the agent — no re-bootstrap, no lost conversation.
   * Used by hosts with a live config editor (e.g. the Studio preview).
   */
  update(config: Partial<WidgetAppearance>): void;
  /** Unmount the widget, end the session, and remove the host element. */
  destroy(): void;
  /** The underlying headless agent, for advanced host integrations. */
  readonly agent: WebsiteAgent;
}
