import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionPaneCard } from "../src/components/chat/session-pane-card";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function render(element: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(element);
  });
  return container;
}

describe("SessionPaneCard", () => {
  it("shows its title and subtitle", () => {
    const node = render(
      <SessionPaneCard
        title="Browser"
        subtitle="Navigate to HotNews.ro"
        onOpen={vi.fn()}
      />,
    );
    expect(node.textContent).toContain("Browser");
    expect(node.textContent).toContain("Navigate to HotNews.ro");
  });

  it("opens on click", async () => {
    const onOpen = vi.fn();
    const node = render(<SessionPaneCard title="Browser" onOpen={onOpen} />);
    await act(async () => {
      node.querySelector<HTMLButtonElement>("button")?.click();
    });
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("renders a thumbnail when one is supplied", () => {
    const node = render(
      <SessionPaneCard
        title="Browser"
        thumbnail="data:image/png;base64,AAA"
        onOpen={vi.fn()}
      />,
    );
    const image = node.querySelector<HTMLImageElement>(
      '[data-testid="session-pane-card-thumb"]',
    );
    expect(image?.getAttribute("src")).toBe("data:image/png;base64,AAA");
  });

  it("falls back to the icon when there is no thumbnail yet", () => {
    // The preview endpoint is a poll, so the first render has nothing to show
    // and must not leave a broken image in the card.
    const node = render(<SessionPaneCard title="Browser" onOpen={vi.fn()} />);
    expect(
      node.querySelector('[data-testid="session-pane-card-thumb"]'),
    ).toBeNull();
    expect(
      node.querySelector('[data-testid="session-pane-card-icon"]'),
    ).not.toBeNull();
  });

  it("shows a count when one is given, and hides it at zero", () => {
    const withCount = render(
      <SessionPaneCard title="Files" count={3} onOpen={vi.fn()} />,
    );
    expect(withCount.textContent).toContain("3");

    act(() => {
      root?.render(<SessionPaneCard title="Files" count={0} onOpen={vi.fn()} />);
    });
    // A zero badge is noise: an empty workspace says nothing worth a chip.
    expect(container?.textContent).not.toContain("0");
  });

  it("is a real button, so it is reachable by keyboard", () => {
    const node = render(<SessionPaneCard title="Files" onOpen={vi.fn()} />);
    const button = node.querySelector("button");
    expect(button).not.toBeNull();
    expect(button?.getAttribute("type")).toBe("button");
    expect(button?.getAttribute("aria-label")).toContain("Files");
  });
});
