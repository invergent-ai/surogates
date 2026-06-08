/**
 * Open a provider OAuth / connect URL in a centered popup window (not a new
 * tab) and return the window handle so the caller can poll ``closed`` and
 * ``close()`` it once the flow completes. ``noopener`` is intentionally
 * omitted — it would force ``window.open`` to return ``null`` (no handle);
 * the popup only ever navigates to the trusted Composio connect domain.
 * Returns ``null`` when the popup is blocked (caller falls back to a tab).
 */
export function openOAuthPopup(url: string): Window | null {
  const w = 600;
  const h = 720;
  const dualLeft = window.screenLeft ?? window.screenX ?? 0;
  const dualTop = window.screenTop ?? window.screenY ?? 0;
  const width = window.innerWidth || document.documentElement.clientWidth || w;
  const height =
    window.innerHeight || document.documentElement.clientHeight || h;
  const left = dualLeft + Math.max(0, (width - w) / 2);
  const top = dualTop + Math.max(0, (height - h) / 2);
  const features = `popup=yes,width=${w},height=${h},left=${left},top=${top}`;
  const popup = window.open(url, "composio-oauth", features);
  if (!popup) {
    // Popup blocked — fall back so the flow still completes.
    window.open(url, "_blank", "noopener,noreferrer");
  }
  return popup;
}

/** True for Composio's hosted connect/authentication links. */
export function isComposioConnectUrl(url: string | undefined): boolean {
  if (!url) return false;
  try {
    return new URL(url).hostname === "connect.composio.dev";
  } catch {
    return false;
  }
}
