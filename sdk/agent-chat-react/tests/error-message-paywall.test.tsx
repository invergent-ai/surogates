import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { ErrorMessage } from "../src/components/chat/error-message";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
});

function render(node: React.ReactNode): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(node);
  });
  return container;
}

describe("ErrorMessage paywall card", () => {
  it("renders the buy link as a real, new-tab anchor", () => {
    const el = render(
      <ErrorMessage
        errorInfo={{
          category: "paywall",
          title: "You've reached your usage limit for this assistant.",
          detail: "",
          retryable: false,
          paywall: {
            code: "allowance_exhausted",
            buyUrl: "https://example.test/buy/agent",
          },
        }}
      />,
    );

    expect(el.textContent).toContain(
      "You've reached your usage limit for this assistant.",
    );
    const link = el.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.test/buy/agent");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(el.textContent).not.toContain("Retry");
  });

  it("offers no link when the owner, not the visitor, has to act", () => {
    const el = render(
      <ErrorMessage
        errorInfo={{
          category: "paywall",
          title: "This assistant is temporarily unavailable.",
          detail: "",
          retryable: false,
          paywall: { code: "operator_subscription_exhausted" },
        }}
      />,
    );

    expect(el.querySelector("a")).toBeNull();
    expect(el.textContent).toContain("temporarily unavailable");
  });
});
