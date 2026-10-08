import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { useSmoothStream } from "../src/components/chat/use-smooth-stream";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  delete document.documentElement.dataset.motion;
});

function Shown({ text, streaming }: { text: string; streaming: boolean }) {
  return <p>{useSmoothStream(text, streaming)}</p>;
}

function render(text: string, streaming: boolean): HTMLElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root!.render(<Shown text={text} streaming={streaming} />));
  return container;
}

it("reveals a streaming answer as it comes, not all at once", () => {
  expect(render("Here is the summary of the quarter.", true).textContent).toBe("");
});

it("shows a streaming answer whole where the host reduces motion", () => {
  document.documentElement.dataset.motion = "reduced";
  expect(render("Here is the summary of the quarter.", true).textContent).toBe("Here is the summary of the quarter.");
});
