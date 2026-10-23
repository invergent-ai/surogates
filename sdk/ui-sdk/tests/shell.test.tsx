/**
 * The app chrome a page sits in: its header, the phone top bar and the
 * navigation drawer the bar opens.
 *
 * happy-dom's viewport is 1024px, so the phone branch is forced by stubbing
 * `matchMedia` per test; without it every test would only see the desktop.
 */
import { act, type ReactNode, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MobileTopBar } from "../src/mobile-top-bar";
import { NavDrawer } from "../src/nav-drawer";
import { PageHeader } from "../src/page-header";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
const realMatchMedia = window.matchMedia;

/** A phone matches the `max-width` query; a desktop matches nothing. */
function viewport(kind: "phone" | "desktop") {
  window.matchMedia = ((query: string) => ({
    matches: kind === "phone" && query.includes("max-width"),
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

function render(ui: ReactNode): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(ui));
  return container;
}

function rerender(ui: ReactNode) {
  act(() => root?.render(ui));
}

function buttonNamed(name: string): HTMLButtonElement | null {
  return (
    Array.from(document.querySelectorAll("button")).find(
      (b) => (b.getAttribute("aria-label") ?? b.textContent?.trim()) === name,
    ) ?? null
  );
}

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  window.matchMedia = realMatchMedia;
});

describe("the page chrome", () => {
  it("the page header shows its title, subtitle and actions on a desktop", () => {
    viewport("desktop");
    const view = render(
      <PageHeader
        title="Cases"
        subtitle="Every open case"
        right={<button type="button">New case</button>}
      />,
    );

    const header = view.querySelector("header");
    expect(header?.querySelector("h1")?.textContent).toBe("Cases");
    expect(header?.textContent).toContain("Every open case");
    expect(header?.contains(buttonNamed("New case"))).toBe(true);
  });

  it("on a phone the header's actions move into the top bar", () => {
    viewport("phone");
    const page = (stack: boolean) => (
      <>
        <MobileTopBar title="Cases" onOpenSidebar={() => undefined} />
        <PageHeader
          title="Cases"
          stackRightOnMobile={stack}
          right={<button type="button">New case</button>}
        />
      </>
    );
    const view = render(page(false));

    const slot = view.querySelector("#mobile-page-actions");
    const header = view.querySelector("header");
    expect(slot?.contains(buttonNamed("New case"))).toBe(true);
    expect(header?.contains(buttonNamed("New case"))).toBe(false);

    rerender(page(true));

    expect(header?.contains(buttonNamed("New case"))).toBe(true);
    expect(slot?.contains(buttonNamed("New case"))).toBe(false);
  });

  it("the top bar's menu button opens the drawer, and its close button closes it", () => {
    viewport("phone");
    const onOpenChange = vi.fn();
    function Shell() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <MobileTopBar title="Cases" onOpenSidebar={() => setOpen(true)} />
          <NavDrawer
            open={open}
            onOpenChange={(next) => {
              onOpenChange(next);
              setOpen(next);
            }}
          >
            <a href="/cases">All cases</a>
          </NavDrawer>
        </>
      );
    }
    render(<Shell />);
    expect(buttonNamed("Close navigation")).toBeNull();

    act(() => buttonNamed("Open navigation")?.click());

    const drawer = document.querySelector('[role="dialog"]');
    expect(drawer?.getAttribute("data-state")).toBe("open");
    expect(drawer?.textContent).toContain("All cases");
    const close = buttonNamed("Close navigation");
    expect(drawer?.contains(close)).toBe(true);

    act(() => close?.click());

    expect(onOpenChange).toHaveBeenCalledWith(false);
    // The panel stays mounted while it slides out, and happy-dom never ends
    // the slide, so its state is what shows it closed.
    expect(drawer?.getAttribute("data-state")).toBe("closed");
  });

  it("the brand mark links home through the app's own link", () => {
    viewport("phone");
    const view = render(
      <MobileTopBar
        title="Cases"
        onOpenSidebar={() => undefined}
        home={<a href="/home" />}
      />,
    );

    const home = view.querySelector('a[aria-label="Home"]');
    expect(home?.getAttribute("href")).toBe("/home");
    expect(home?.querySelector("img")).not.toBeNull();
  });
});
