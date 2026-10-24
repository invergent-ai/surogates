// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A value shown to copy: a read-only field named by its label, Copy that puts
// the value on the clipboard or says how to copy it by hand, and a secret
// masked until shown, that nothing but the field holds.
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyField } from "../src/copy-field";
import {
  buttonNamed,
  cleanup,
  click,
  controlFor,
  fieldOf,
  render,
  rerender,
  settle,
} from "./dom";

const ADDRESS = "https://app.example/p/p1/work";
const KEY = "example-service-key-0123456789";
const REFUSED = "The browser did not let this be copied. Select it and copy it.";

/** Puts `clipboard` on the page's navigator in place of the browser's. */
function withClipboard(
  clipboard: { writeText: (text: string) => Promise<void> } | undefined,
): void {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: clipboard,
  });
}

afterEach(() => {
  cleanup();
  // The own property shadows the browser's; removing it brings that back.
  delete (navigator as { clipboard?: unknown }).clipboard;
});

function button(root: ParentNode, name: string): HTMLButtonElement {
  const found = buttonNamed(root, name);
  if (!found) throw new Error(`no button "${name}"`);
  return found;
}

function inputFor(root: ParentNode, label: string): HTMLInputElement {
  const control = controlFor(root, label);
  if (!(control instanceof HTMLInputElement)) {
    throw new Error(`the label "${label}" is not for an input`);
  }
  return control;
}

describe("copy field", () => {
  it("the label names a read-only field holding the value", () => {
    const view = render(
      <CopyField
        label="Business app"
        value={ADDRESS}
        description="Send it to the people who work the cases."
      />,
    );

    const input = inputFor(view, "Business app");
    expect(input.readOnly).toBe(true);
    expect(input.value).toBe(ADDRESS);
    expect(input.type).not.toBe("password");
    expect(fieldOf(view, "Business app").textContent).toContain(
      "Send it to the people who work the cases.",
    );
  });

  it("copy puts the value on the clipboard and says so", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    withClipboard({ writeText });
    const view = render(<CopyField label="Business app" value={ADDRESS} />);

    click(button(view, "Copy"));
    await settle();

    expect(writeText).toHaveBeenCalledWith(ADDRESS);
    expect(buttonNamed(view, "Copied")).not.toBeNull();
    expect(buttonNamed(view, "Copy")).toBeNull();
  });

  it("a secret stays hidden until shown", () => {
    const view = render(<CopyField label="Service key" value={KEY} secret />);
    const input = inputFor(view, "Service key");

    expect(input.type).toBe("password");
    expect(input.autocomplete).toBe("off");

    click(button(view, "Show"));
    expect(inputFor(view, "Service key").type).toBe("text");
    click(button(view, "Hide"));
    expect(inputFor(view, "Service key").type).toBe("password");
    expect(buttonNamed(view, "Show")).not.toBeNull();

    for (const el of view.querySelectorAll("*")) {
      if (el === input) continue;
      for (const attribute of el.attributes) {
        expect(attribute.value).not.toContain(KEY);
      }
    }
  });

  it("a refused copy says how to copy by hand, until a copy works", async () => {
    const writeText = vi.fn().mockRejectedValueOnce(new Error("denied"));
    withClipboard({ writeText });
    const view = render(<CopyField label="Service key" value={KEY} secret />);
    // The status is there, empty, before anything is refused, so a screen
    // reader announces the words when they arrive.
    const status = fieldOf(view, "Service key").querySelector("[role=status]");
    expect(status?.textContent).toBe("");

    click(button(view, "Copy"));
    await settle();

    expect(status?.textContent).toBe(REFUSED);
    expect(buttonNamed(view, "Copy")).not.toBeNull();

    writeText.mockResolvedValueOnce(undefined);
    click(button(view, "Copy"));
    await settle();

    expect(status?.textContent).toBe("");
    expect(view.textContent).not.toContain(REFUSED);
    expect(buttonNamed(view, "Copied")).not.toBeNull();
  });

  it("without a clipboard, the field says how to copy by hand", async () => {
    withClipboard(undefined);
    const view = render(<CopyField label="Business app" value={ADDRESS} />);

    click(button(view, "Copy"));
    await settle();

    expect(
      fieldOf(view, "Business app").querySelector("[role=status]")?.textContent,
    ).toBe(REFUSED);
    expect(buttonNamed(view, "Copy")).not.toBeNull();
  });

  it("a new value starts hidden, uncopied and without a refusal", async () => {
    const writeText = vi
      .fn()
      .mockRejectedValueOnce(new Error("denied"))
      .mockResolvedValueOnce(undefined);
    withClipboard({ writeText });
    const view = render(<CopyField label="Service key" value={KEY} secret />);

    click(button(view, "Show"));
    click(button(view, "Copy"));
    await settle();
    expect(view.textContent).toContain(REFUSED);

    rerender(
      <CopyField label="Service key" value="example-service-key-next" secret />,
    );
    expect(inputFor(view, "Service key").type).toBe("password");
    expect(buttonNamed(view, "Show")).not.toBeNull();
    expect(view.textContent).not.toContain(REFUSED);

    click(button(view, "Copy"));
    await settle();
    expect(buttonNamed(view, "Copied")).not.toBeNull();

    rerender(<CopyField label="Service key" value={KEY} secret />);
    expect(buttonNamed(view, "Copy")).not.toBeNull();
    expect(buttonNamed(view, "Copied")).toBeNull();
  });
});
