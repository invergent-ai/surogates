import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { BrowserActivityGroup } from "../src/components/browser/browser-activity-group";
import type { ToolCallInfo } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const calls: ToolCallInfo[] = [
  {
    id: "1",
    toolName: "browser_navigate",
    args: "{\"url\":\"https://app.com\"}",
    result: "{\"url\":\"https://app.com\"}",
    status: "complete",
  },
  {
    id: "2",
    toolName: "browser_click",
    args: "{\"ref\":\"@e3\"}",
    result: "{\"clicked\":true}",
    status: "complete",
  },
  {
    id: "3",
    toolName: "browser_type",
    args: "{\"ref\":\"@e4\",\"text\":\"x\"}",
    result: "{\"typed\":true}",
    status: "complete",
  },
];

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

function renderGroup(nextCalls: ToolCallInfo[]) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<BrowserActivityGroup calls={nextCalls} />);
  });
  return container;
}

describe("BrowserActivityGroup", () => {
  it("renders all actions inline, joined by commas", () => {
    const node = renderGroup(calls);

    expect(node.textContent).toContain("Browser:");
    expect(node.textContent).toContain("navigate to https://app.com");
    expect(node.textContent).toContain("click @e3");
    expect(node.textContent).toContain("type 1 char into @e4");
    expect(node.textContent).toMatch(
      /navigate to https:\/\/app\.com, click @e3, type 1 char into @e4/,
    );
  });

  // An event stored before the server masked typed text still carries the
  // password in the clear; the row must not show it.
  it("shows how much was typed, never the text", () => {
    const node = renderGroup([
      {
        id: "5",
        toolName: "browser_type",
        args: "{\"ref\":\"@e5\",\"text\":\"Waalhaven-2026\"}",
        result: "{\"typed\":true}",
        status: "complete",
      },
      {
        id: "6",
        toolName: "browser_type",
        args: "{\"ref\":\"@e6\",\"secret_ref\":\"vault://browser_acme\"}",
        result: "{\"typed\":true}",
        status: "complete",
      },
    ]);

    expect(node.textContent).not.toContain("Waalhaven");
    expect(node.textContent).toContain("type 14 chars into @e5");
    expect(node.textContent).toContain("type a stored secret into @e6");
  });

  it("flags erroring actions with a marker", () => {
    const node = renderGroup([
      ...calls,
      {
        id: "4",
        toolName: "browser_click",
        args: "{}",
        result: "{\"error\":\"paused_by_user\"}",
        status: "error",
      },
    ]);

    expect(node.querySelector('[data-testid="activity-error-4"]')).not.toBeNull();
  });
});
