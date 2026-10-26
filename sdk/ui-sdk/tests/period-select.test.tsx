// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A period picked from a short list of the last days: the words a period
// reads as, a combobox named by the visible label, and a period that is not
// one of the choices still shown by its words.
import { act, useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { PERIOD_CHOICES, PeriodSelect, periodWords } from "../src/period-select";
import { cleanup, click, render, settle } from "./dom";

afterEach(cleanup);

/** The combobox of `root` and the words it reads. */
function periodOf(root: ParentNode): { name: string; shows: string } {
  const combobox = root.querySelector<HTMLElement>("[role=combobox]");
  if (!combobox) throw new Error("no combobox");
  const labelledBy = combobox.getAttribute("aria-labelledby") ?? "";
  const name = labelledBy
    .split(/\s+/)
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ")
    .trim();
  return { name, shows: combobox.textContent?.trim() ?? "" };
}

/** Opens the select the way the keyboard does, and lists its options. */
async function open(root: ParentNode): Promise<HTMLElement[]> {
  const combobox = root.querySelector<HTMLElement>("[role=combobox]");
  if (!combobox) throw new Error("no combobox");
  act(() => {
    combobox.focus();
    combobox.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
    );
  });
  await settle();
  return Array.from(document.querySelectorAll<HTMLElement>("[role=option]"));
}

/** A host that keeps the period, and shows the day after its last day. */
function Host({ start }: { start: number }) {
  const [days, setDays] = useState(start);
  return (
    <>
      <PeriodSelect value={days} onChange={setDays} />
      <output>{days + 1}</output>
    </>
  );
}

describe("period select", () => {
  it("a period reads as the last days it covers", () => {
    expect(periodWords(1)).toBe("Last day");
    expect(periodWords(30)).toBe("Last 30 days");
    expect(PERIOD_CHOICES).toEqual([7, 30, 90]);
  });

  it("a period that is a choice reads as its words, beside the label", () => {
    const view = render(<PeriodSelect value={30} onChange={() => {}} />);

    expect(view.textContent).toContain("Period");
    expect(periodOf(view)).toEqual({ name: "Period", shows: "Last 30 days" });
  });

  it("a period that is not a choice still shows, named by its label", () => {
    const view = render(<PeriodSelect value={14} onChange={() => {}} />);

    expect(view.textContent).toContain("Period");
    expect(periodOf(view)).toEqual({ name: "Period", shows: "Last 14 days" });
  });

  it("a label given names the select in its place", () => {
    const view = render(
      <PeriodSelect value={7} onChange={() => {}} label="Show" />,
    );

    expect(periodOf(view)).toEqual({ name: "Show", shows: "Last 7 days" });
  });

  it("a period that is not a choice sits in its numeric place among them", async () => {
    const view = render(<PeriodSelect value={14} onChange={() => {}} />);
    const options = await open(view);

    expect(options.map((option) => option.textContent)).toEqual([
      "Last 7 days",
      "Last 14 days",
      "Last 30 days",
      "Last 90 days",
    ]);
  });

  it("the choices given replace the usual ones, one option each", async () => {
    const view = render(
      <PeriodSelect value={1} onChange={() => {}} choices={[1, 14]} />,
    );
    const options = await open(view);

    expect(options.map((option) => option.textContent)).toEqual([
      "Last day",
      "Last 14 days",
    ]);
  });

  it("a period picked shows, and is handed over as a number", async () => {
    const view = render(<Host start={30} />);
    const options = await open(view);
    const ninety = options.find((option) => option.textContent === "Last 90 days");
    if (!ninety) throw new Error("no option for 90 days");
    click(ninety);
    await settle();

    expect(periodOf(view).shows).toBe("Last 90 days");
    // A number adds up to 91; the text "90" would read "901".
    expect(view.querySelector("output")?.textContent).toBe("91");
  });
});
