// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// What a test does to the page: mount a component, click, type, pick an
// option, wait for React to settle, and find a field, a control or a button
// the way a person would, by the words on it. Every test file calls
// `cleanup` in `afterEach`.
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const mounted: { root: Root; container: HTMLDivElement }[] = [];

/** Mounts `ui` in a fresh container on the page and returns the container. */
export function render(ui: ReactNode): HTMLElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  act(() => root.render(ui));
  return container;
}

/** Renders `ui` in place of what the last `render` mounted. */
export function rerender(ui: ReactNode): void {
  const last = mounted[mounted.length - 1];
  if (!last) throw new Error("rerender before render");
  act(() => last.root.render(ui));
}

/** Unmounts everything `render` mounted and empties the page. */
export function cleanup(): void {
  for (const { root, container } of mounted.splice(0)) {
    act(() => root.unmount());
    container.remove();
  }
  document.body.innerHTML = "";
}

export function click(el: Element): void {
  act(() => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  });
}

/**
 * Sets a field's text the way a browser does. React tracks the value it last
 * wrote, so the native setter has to bypass it, or the `input` event would
 * look like no change at all.
 */
export function typeInto(
  el: HTMLInputElement | HTMLTextAreaElement,
  text: string,
): void {
  const proto =
    el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  act(() => {
    setter?.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

/** Picks the option of a native `<select>` whose value is `value`. */
export function choose(el: HTMLSelectElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLSelectElement.prototype,
    "value",
  )?.set;
  act(() => {
    setter?.call(el, value);
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

/** Lets pending effects and promises run. */
export async function settle(): Promise<void> {
  await act(async () => {});
}

/** A label's words, without the `*` that marks a required field. */
function labelWords(el: Element): string {
  return (el.textContent ?? "").trim().replace(/\s*\*$/, "");
}

/** The field whose own label reads `label`. */
export function fieldOf(root: ParentNode, label: string): HTMLElement {
  const field = Array.from(
    root.querySelectorAll<HTMLElement>("[data-slot=field]"),
  ).find((candidate) =>
    Array.from(
      candidate.querySelectorAll("label, [data-slot=field-label]"),
    ).some(
      (el) =>
        el.closest("[data-slot=field]") === candidate &&
        labelWords(el) === label,
    ),
  );
  if (!field) throw new Error(`no field labelled "${label}"`);
  return field;
}

/** The control that the label reading `label` is for. */
export function controlFor(root: ParentNode, label: string): HTMLElement {
  const el = Array.from(root.querySelectorAll("label")).find(
    (candidate) => labelWords(candidate) === label,
  );
  if (!el) throw new Error(`no label "${label}"`);
  const id = el.getAttribute("for");
  if (!id) throw new Error(`the label "${label}" names no control`);
  const control = Array.from(root.querySelectorAll<HTMLElement>("[id]")).find(
    (candidate) => candidate.id === id,
  );
  if (!control) throw new Error(`no control for the label "${label}"`);
  return control;
}

/** The button whose accessible name, or else its text, is `name`. */
export function buttonNamed(
  root: ParentNode,
  name: string,
): HTMLButtonElement | null {
  return (
    Array.from(root.querySelectorAll("button")).find(
      (b) => (b.getAttribute("aria-label") ?? b.textContent?.trim()) === name,
    ) ?? null
  );
}
