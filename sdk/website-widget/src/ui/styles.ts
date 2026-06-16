/**
 * Scoped CSS for the chat widget, injected into the Shadow DOM root so
 * it can neither leak into nor be overridden by the host page.  All
 * colours derive from two custom properties (``--surg-accent`` and its
 * readable foreground) set at mount time from the caller's
 * ``accentColor`` so theming is a one-line change.
 *
 * Returned as a string (rather than a constructable stylesheet) because
 * a ``<style>`` element in the shadow root is the most broadly supported
 * path across the browsers a public embed has to serve.
 */
export const WIDGET_STYLES = `
:host { all: initial; }
* { box-sizing: border-box; }

.surg-root {
  --surg-radius: 16px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  color: #0f172a;
}

.surg-launcher {
  position: fixed;
  bottom: 20px;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  border: none;
  cursor: pointer;
  background: var(--surg-accent);
  color: var(--surg-accent-fg);
  box-shadow: 0 6px 24px rgba(15, 23, 42, 0.25);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  z-index: 2147483000;
}
.surg-launcher:hover { transform: scale(1.06); box-shadow: 0 8px 28px rgba(15, 23, 42, 0.3); }
.surg-launcher svg { width: 26px; height: 26px; }
.surg-pos-bottom-right { right: 20px; }
.surg-pos-bottom-left { left: 20px; }

.surg-panel {
  position: fixed;
  bottom: 88px;
  width: 380px;
  max-width: calc(100vw - 40px);
  height: 560px;
  max-height: calc(100vh - 120px);
  background: #ffffff;
  border-radius: var(--surg-radius);
  box-shadow: 0 12px 48px rgba(15, 23, 42, 0.28);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  z-index: 2147483000;
  animation: surg-rise 0.18s ease;
}
@keyframes surg-rise { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }

.surg-inline {
  position: relative;
  bottom: auto; right: auto; left: auto;
  width: 100%;
  height: 100%;
  box-shadow: none;
  border: 1px solid #e2e8f0;
  animation: none;
}

.surg-header {
  background: var(--surg-accent);
  color: var(--surg-accent-fg);
  padding: 14px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.surg-header-id { display: flex; align-items: center; gap: 10px; min-width: 0; }
.surg-header-text { display: flex; flex-direction: column; line-height: 1.25; min-width: 0; }
.surg-title { font-weight: 600; font-size: 15px; }
.surg-subtitle { font-size: 11px; font-weight: 400; opacity: 0.85; }
.surg-logo {
  width: 32px; height: 32px; border-radius: 50%; object-fit: cover;
  background: rgba(255, 255, 255, 0.2); flex-shrink: 0;
}
.surg-launcher-logo {
  width: 32px; height: 32px; border-radius: 50%; object-fit: cover;
}
.surg-close {
  background: transparent;
  border: none;
  color: inherit;
  cursor: pointer;
  opacity: 0.85;
  padding: 4px;
  display: flex;
  border-radius: 6px;
}
.surg-close:hover { opacity: 1; background: rgba(255, 255, 255, 0.18); }
.surg-close svg { width: 18px; height: 18px; }

.surg-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: #f8fafc;
}

.surg-bubble {
  max-width: 82%;
  padding: 9px 13px;
  border-radius: 14px;
  word-wrap: break-word;
  white-space: normal;
}
.surg-bubble p { margin: 0 0 8px; }
.surg-bubble p:last-child { margin-bottom: 0; }
.surg-bubble ul, .surg-bubble ol { margin: 4px 0; padding-left: 20px; }
.surg-bubble code {
  background: rgba(15, 23, 42, 0.08);
  padding: 1px 5px;
  border-radius: 5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.9em;
}
.surg-bubble pre {
  background: #0f172a;
  color: #e2e8f0;
  padding: 10px 12px;
  border-radius: 10px;
  overflow-x: auto;
  margin: 6px 0;
}
.surg-bubble pre code { background: transparent; padding: 0; color: inherit; }
.surg-bubble a { color: var(--surg-accent); }

.surg-user {
  align-self: flex-end;
  background: var(--surg-accent);
  color: var(--surg-accent-fg);
  border-bottom-right-radius: 4px;
  white-space: pre-wrap;
}
.surg-user a { color: var(--surg-accent-fg); text-decoration: underline; }
.surg-assistant {
  align-self: flex-start;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-bottom-left-radius: 4px;
}

.surg-typing { display: flex; gap: 4px; padding: 4px 2px; }
.surg-typing span {
  width: 7px; height: 7px; border-radius: 50%;
  background: #94a3b8;
  animation: surg-blink 1.2s infinite ease-in-out;
}
.surg-typing span:nth-child(2) { animation-delay: 0.2s; }
.surg-typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes surg-blink { 0%, 60%, 100% { opacity: 0.3; } 30% { opacity: 1; } }

.surg-error {
  margin: 0 16px;
  padding: 8px 12px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #b91c1c;
  border-radius: 10px;
  font-size: 13px;
  flex-shrink: 0;
}

.surg-composer {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid #e2e8f0;
  background: #ffffff;
  flex-shrink: 0;
}
.surg-input {
  flex: 1;
  resize: none;
  border: 1px solid #cbd5e1;
  border-radius: 12px;
  padding: 9px 12px;
  font: inherit;
  color: inherit;
  max-height: 120px;
  outline: none;
}
.surg-input:focus { border-color: var(--surg-accent); }
.surg-send {
  border: none;
  background: var(--surg-accent);
  color: var(--surg-accent-fg);
  border-radius: 12px;
  width: 40px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.surg-send:disabled { opacity: 0.5; cursor: not-allowed; }
.surg-send svg { width: 18px; height: 18px; }

.surg-powered {
  text-align: center;
  font-size: 11px;
  color: #94a3b8;
  padding: 0 0 8px;
  background: #ffffff;
}
.surg-powered a { color: inherit; font-weight: 500; text-decoration: none; }
.surg-powered a:hover { text-decoration: underline; }
`;
