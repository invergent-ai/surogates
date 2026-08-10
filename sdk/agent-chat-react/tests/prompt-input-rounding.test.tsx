/**
 * The composer box keeps its rounded corners.
 *
 * Two separate things erase them, and both have bitten:
 *
 * 1. InputGroup's own base class carries `rounded-none` under
 *    `has-data-[align=…]` variants. tailwind-merge only drops a base class
 *    when the override repeats the *same* variant key, so overriding just
 *    the `has-[>[data-align=…]]` spelling leaves both `rounded-none` and
 *    `rounded-3xl` on the element and stylesheet order decides the winner.
 * 2. The border beam wraps the box in `overflow: hidden` plus its own
 *    radius. A clip radius wider than the box's corner cuts the whole arc
 *    away, leaving four straight edges that stop short of each corner —
 *    which is what "the corners disappeared" looks like. The beam measures
 *    the child, so no literal radius may be passed.
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  PromptInputTextarea,
} from "../src/components/ai-elements/prompt-input";

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

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(node));
  return container;
}

function mountComposer(beamActive: boolean): HTMLDivElement {
  return mount(
    <PromptInput onSubmit={() => {}} beamActive={beamActive}>
      <PromptInputBody>
        <PromptInputTextarea placeholder="Send a message..." />
      </PromptInputBody>
      <PromptInputFooter>
        <button type="button">send</button>
      </PromptInputFooter>
    </PromptInput>,
  );
}

describe("Composer rounding", () => {
  it("leaves no rounded-none on the box, in any variant", () => {
    const box = mountComposer(false).querySelector<HTMLElement>(
      "[data-slot='input-group']",
    )!;
    expect(box.className).toContain("rounded-3xl");
    expect(box.className).not.toContain("rounded-none");
  });

  it("rounds the box whether the footer is matched as a child or a descendant", () => {
    const box = mountComposer(false).querySelector<HTMLElement>(
      "[data-slot='input-group']",
    )!;
    // `has-data-[align=…]` matches any descendant, `has-[>[data-align=…]]`
    // only a direct child. Both spellings must round, or a future wrapper
    // around the footer silently squares the box off.
    for (const variant of [
      "has-data-[align=block-end]:rounded-3xl",
      "has-data-[align=block-start]:rounded-3xl",
      "has-[>[data-align=block-end]]:rounded-3xl",
      "has-[>[data-align=block-start]]:rounded-3xl",
      "has-[textarea]:rounded-3xl",
    ]) {
      expect(box.className).toContain(variant);
    }
  });

  it("lets the beam measure the box instead of clipping it to a fixed radius", () => {
    const wrapper = mountComposer(true).querySelector<HTMLElement>("[data-beam]")!;
    // jsdom reports no radius for the child, so the beam falls back to its
    // preset — the assertion that matters is that nothing overrode the
    // measurement with a literal, which is what mismatched the box.
    expect(wrapper.style.borderRadius).toBe("");
    expect(document.querySelector("style")?.textContent ?? "").not.toContain(
      "border-radius: 24px",
    );
  });
});
