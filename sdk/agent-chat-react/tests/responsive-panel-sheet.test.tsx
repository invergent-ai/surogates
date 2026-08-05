/**
 * The phone form of ResponsivePanel.
 *
 * The test environment's viewport is 1024px, so every other test in this
 * package exercises the popover branch only — which is how a bug that
 * stripped the sheet's safe-area inset shipped with a green suite. These
 * force the mobile branch and assert the things that silently break there.
 */
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ResponsivePanel } from "../src/components/ui/responsive-panel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
const realMatchMedia = window.matchMedia;

/** Reports every query as matching, which is what a phone viewport does here. */
function forceMobile(matches: boolean) {
  window.matchMedia = ((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

beforeEach(() => forceMobile(true));

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  window.matchMedia = realMatchMedia;
});

function Harness({ popoverClassName }: { popoverClassName?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <ResponsivePanel
      open={open}
      onOpenChange={setOpen}
      title="Panel title"
      popoverClassName={popoverClassName}
      trigger={
        <button type="button" aria-label="Open panel">
          open
        </button>
      }
    >
      <p>panel body</p>
    </ResponsivePanel>
  );
}

function mountAndOpen(popoverClassName?: string): HTMLElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<Harness popoverClassName={popoverClassName} />);
  });
  const trigger = container.querySelector(
    '[aria-label="Open panel"]',
  ) as HTMLElement;
  act(() => trigger.click());
  const sheet = document.querySelector('[data-slot="responsive-panel-sheet"]');
  if (!sheet) throw new Error("sheet did not render");
  return sheet as HTMLElement;
}

describe("ResponsivePanel on a phone", () => {
  it("renders the sheet rather than the popover", () => {
    const sheet = mountAndOpen();
    expect(sheet.textContent).toContain("panel body");
    expect(
      document.querySelector('[data-slot="popover-content"]'),
    ).toBeNull();
  });

  it("keeps the home-indicator inset whatever the popover is styled with", () => {
    // `cn` is tailwind-merge: a popover `p-0` shares a conflict group with the
    // sheet's `pb-[env(safe-area-inset-bottom)]` and used to delete it, which
    // put the panel's last row under the home indicator. Popover styling must
    // not reach the sheet at all.
    const sheet = mountAndOpen("min-w-60 divide-y p-0");
    expect(sheet.className).toContain("pb-[env(safe-area-inset-bottom)]");
    expect(sheet.className).not.toContain("p-0");
    expect(sheet.className).not.toContain("divide-y");
  });

  it("names itself for screen readers and opts out of the description", () => {
    const sheet = mountAndOpen();
    expect(sheet.getAttribute("aria-describedby")).toBeNull();
    const labelledBy = sheet.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy as string)?.textContent).toBe(
      "Panel title",
    );
  });

  it("uses the popover with a cursor", () => {
    forceMobile(false);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(<Harness popoverClassName="min-w-60" />);
    });
    const trigger = container.querySelector(
      '[aria-label="Open panel"]',
    ) as HTMLElement;
    act(() => trigger.click());
    expect(
      document.querySelector('[data-slot="responsive-panel-sheet"]'),
    ).toBeNull();
    expect(document.body.textContent).toContain("panel body");
  });
});
