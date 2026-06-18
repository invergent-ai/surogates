// @novnc/novnc@1.7 exposes the RFB class at the bare specifier (its package.json
// "exports" maps the package root to "./core/rfb.js"). The published
// @types/novnc__novnc only declares the legacy "@novnc/novnc/lib/rfb" subpath,
// which does not match the bare runtime export — so we declare the small surface
// of the RFB client we actually use here. This file is pulled in via a
// triple-slash reference from browser-live-view.tsx so it resolves in every
// package that compiles that source (e.g. example-chat-app), not just this one.
declare module "@novnc/novnc" {
  export interface NoVncClient {
    viewOnly: boolean;
    scaleViewport: boolean;
    addEventListener(type: string, listener: (event: Event) => void): void;
    removeEventListener(type: string, listener: (event: Event) => void): void;
    disconnect(): void;
  }

  const RFB: new (
    target: Element,
    url: string,
    options?: { wsProtocols?: string[] },
  ) => NoVncClient;

  export default RFB;
}
