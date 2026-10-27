import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { MessageResponse } from "../src/components/ai-elements/message";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function render(markdown: string): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<MessageResponse>{markdown}</MessageResponse>);
  });
  return container;
}

describe("a message's links", () => {
  it("shows a link the chat will not open as its words alone, with no marker, and keeps a web link a link", () => {
    const shown = render("Started the thread [Surogate brand style guide](thread/6241b690). See [the site](https://surogate.ai/).");
    expect(shown.textContent).toContain("Started the thread Surogate brand style guide.");
    expect(shown.textContent).not.toContain("[blocked]");
    expect([...shown.querySelectorAll("a")].map((a) => a.getAttribute("href"))).toEqual(["https://surogate.ai/"]);
  });
});

describe("a message's line breaks", () => {
  it("keeps a single newline a line break, not a space joining two lines", () => {
    const shown = render("Ana\nBob");
    expect(shown.querySelectorAll("br")).toHaveLength(1);
    expect(shown.textContent).not.toContain("Ana Bob");
  });

  it("leaves code blocks and lists as they were", () => {
    const shown = render("Steps:\n\n- one\n- two\n\n```\na\nb\n```");
    expect([...shown.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["one", "two"]);
    expect(shown.querySelectorAll("li br")).toHaveLength(0);
    // A code block keeps one line per source line, each its own block element.
    expect([...shown.querySelectorAll("pre code > span")].map((line) => line.textContent)).toEqual(["a", "b"]);
    expect(shown.querySelectorAll("pre br")).toHaveLength(0);
  });
});
