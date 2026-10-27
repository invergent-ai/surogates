// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A dialog opened from a control gives focus back to that control when it
// closes: whether the page opens it from a trigger or from a state change
// with no trigger at all, which is how most pages open theirs.
import { act, useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "../src/dialog";
import { buttonNamed, cleanup, click, render, settle } from "./dom";

afterEach(cleanup);

/** Lets Radix hand focus back, which it does a tick after the dialog unmounts. */
async function afterClose(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
}

function button(name: string): HTMLButtonElement {
  const found = buttonNamed(document.body, name);
  if (!found) throw new Error(`no button "${name}"`);
  return found;
}

function Controlled() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>See answers</button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Answers</DialogTitle>
          <DialogDescription>How people answered</DialogDescription>
        </DialogContent>
      </Dialog>
    </>
  );
}

function WithTrigger() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button>Open</button>
      </DialogTrigger>
      <DialogContent>
        <DialogTitle>Details</DialogTitle>
        <DialogDescription>Everything about it</DialogDescription>
      </DialogContent>
    </Dialog>
  );
}

describe("a dialog's focus on close", () => {
  it("goes back to the control that opened a dialog with no trigger", async () => {
    render(<Controlled />);
    const opener = button("See answers");
    act(() => opener.focus());
    click(opener);
    await settle();
    expect(document.activeElement).not.toBe(opener);

    click(button("Close"));
    await afterClose();

    expect(document.activeElement).toBe(opener);
  });

  it("goes back to the trigger of a dialog that has one", async () => {
    render(<WithTrigger />);
    const trigger = button("Open");
    act(() => trigger.focus());
    click(trigger);
    await settle();

    click(button("Close"));
    await afterClose();

    expect(document.activeElement).toBe(trigger);
  });

  it("goes to the control that opened it each time it opens, not the first", async () => {
    function Two() {
      const [open, setOpen] = useState<string | null>(null);
      return (
        <>
          <button onClick={() => setOpen("a")}>First</button>
          <button onClick={() => setOpen("b")}>Second</button>
          <Dialog open={open !== null} onOpenChange={() => setOpen(null)}>
            <DialogContent>
              <DialogTitle>One dialog</DialogTitle>
              <DialogDescription>Opened from either</DialogDescription>
            </DialogContent>
          </Dialog>
        </>
      );
    }
    render(<Two />);

    const first = button("First");
    act(() => first.focus());
    click(first);
    await settle();
    click(button("Close"));
    await afterClose();
    expect(document.activeElement).toBe(first);

    const second = button("Second");
    act(() => second.focus());
    click(second);
    await settle();
    click(button("Close"));
    await afterClose();
    expect(document.activeElement).toBe(second);
  });
});
