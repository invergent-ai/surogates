// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A form drawn from a spec: the outcome, titled groups with one input per
// field type and hint, file uploads and picks, errors and check notes, and
// the values a form sends and takes back.
import { act, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type FileValue,
  type FormSpec,
  type FormSpecOutcome,
  type FormValues,
  fromOutput,
  missingInputs,
  SpecForm,
  type SpecFormProps,
  toOutput,
} from "../src/spec-form";
import {
  buttonNamed,
  choose,
  cleanup,
  click,
  controlFor,
  fieldOf,
  render,
  settle,
  typeInto,
} from "./dom";

afterEach(cleanup);

const approve: FormSpecOutcome = {
  key: "approve",
  label: "Approve",
  required: ["amount"],
};
const decline: FormSpecOutcome = { key: "decline", label: "Decline" };

const loanForm: FormSpec = {
  outcome_label: "Decision",
  outcomes: [approve, decline],
  required: ["applicant"],
  checks: ["The amount must not exceed the lending limit."],
  groups: [
    {
      title: "About the applicant",
      help: "As on the application.",
      inputs: [
        { field: "applicant", label: "Applicant", type: "string" },
        { field: "notes", label: "Notes", type: "string", hint: "textarea" },
        { field: "term_years", label: "Term", type: "int", unit: "years" },
        { field: "amount", label: "Amount", type: "decimal", unit: "EUR" },
      ],
    },
    {
      inputs: [
        { field: "signed_on", label: "Signed on", type: "date" },
        { field: "due", label: "Due", type: "datetime" },
        {
          field: "risk",
          label: "Risk",
          type: "choice",
          hint: "radio",
          choices: [
            { value: "low", label: "Low" },
            { value: "high", label: "High" },
          ],
        },
        {
          field: "channel",
          label: "Channel",
          type: "choice",
          choices: [
            { value: "branch", label: "Branch" },
            { value: "online", label: "Online" },
          ],
        },
        { field: "verified", label: "Verified", type: "bool" },
        { field: "urgent", label: "Urgent", type: "bool", hint: "switch" },
        { field: "income", label: "Income", type: "list" },
        { field: "documents", label: "Documents", type: "list", file: "list" },
        { field: "contract", label: "Contract", type: "file", file: "one" },
      ],
    },
  ],
};

const fileA: FileValue = {
  name: "id-card.pdf",
  uri: "case-file://c1/id-card.pdf",
  sha256: "aa11",
};
const fileB: FileValue = {
  name: "payslip.pdf",
  uri: "case-file://c1/payslip.pdf",
  sha256: "bb22",
};

/** Draws `loanForm` (or `form`), with spies for every callback not given. */
function draw(props: Partial<SpecFormProps> = {}) {
  const onChange = vi.fn();
  const onOutcome = vi.fn();
  const root = render(
    <SpecForm
      form={loanForm}
      values={{}}
      onChange={onChange}
      onOutcome={onOutcome}
      {...props}
    />,
  );
  return { root, onChange, onOutcome };
}

/** Where `text` starts in the words `root` shows; -1 when it does not. */
function at(root: Element, text: string): number {
  return (root.textContent ?? "").indexOf(text);
}

/** The innermost element whose words are exactly `text`. */
function wordsElement(root: Element, text: string): Element {
  const el = Array.from(root.querySelectorAll("*")).find(
    (candidate) =>
      candidate.textContent?.trim() === text &&
      Array.from(candidate.children).every(
        (child) => child.textContent?.trim() !== text,
      ),
  );
  if (!el) throw new Error(`no element reads "${text}"`);
  return el;
}

function follows(first: Node, second: Node): boolean {
  return Boolean(
    first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
  );
}

function fileInputOf(root: ParentNode, label: string): HTMLInputElement {
  const input = fieldOf(root, label).querySelector<HTMLInputElement>(
    "input[type=file]",
  );
  if (!input) throw new Error(`no file input in "${label}"`);
  return input;
}

/** Puts `file` in a file input the way a browser's file chooser does. */
async function pickFile(input: HTMLInputElement, file: File): Promise<void> {
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  await act(async () => {
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

/** The words an element's `aria-describedby` points at, joined. */
function describedText(el: Element): string {
  return (el.getAttribute("aria-describedby") ?? "")
    .split(" ")
    .filter(Boolean)
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
}

const TWO_WAYS = "Write the number without thousands separators.";
const NOT_A_NUMBER = "Not a number.";

/**
 * `loanForm` as an app draws it: it keeps what is typed, and shows beside
 * each field what `missingInputs` says of it. Typing into the field labelled
 * `label` then reads what the form would send for `field`, the error shown
 * beside it, and what `missingInputs` says of it.
 */
function typingInto(label: string, field: string) {
  let latest: FormValues = {};
  function Checked() {
    const [values, setValues] = useState<FormValues>({ applicant: "Ana Pop" });
    latest = values;
    return (
      <SpecForm
        form={loanForm}
        values={values}
        onChange={(name, value) =>
          setValues((before) => ({ ...before, [name]: value }))
        }
        errors={{ fields: missingInputs(loanForm, null, values) }}
      />
    );
  }
  const root = render(<Checked />);
  const control = controlFor(root, label) as HTMLInputElement;
  return {
    typing: (text: string) => typeInto(control, text),
    sent: () => toOutput(loanForm, latest)[field],
    beside: () =>
      fieldOf(root, label).querySelector("[data-slot=field-error]")?.textContent ??
      null,
    missing: () => missingInputs(loanForm, null, latest)[field],
  };
}

function pickerOf(root: ParentNode, label: string): HTMLSelectElement | null {
  return root.querySelector<HTMLSelectElement>(
    `select[aria-label="Pick a file for ${label}"]`,
  );
}

describe("a form drawn from a spec", () => {
  it("the outcome comes first, then each group with its title and help", () => {
    const { root } = draw();

    const order = [
      "Decision",
      "Approve",
      "About the applicant",
      "As on the application.",
    ].map((text) => at(root, text));
    expect(order.every((index) => index >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("a start form shows no outcome, and a vote shows only its choices", () => {
    const start = draw({ form: { ...loanForm, outcomes: [] } }).root;
    const firstLegend = start.querySelector("legend");
    expect(firstLegend?.textContent).toBe("About the applicant");
    const cards = Array.from(
      start.querySelectorAll("[data-slot=choice-card]"),
    );
    expect(cards.length).toBeGreaterThan(0);
    expect(cards.every((card) => follows(firstLegend as Node, card))).toBe(
      true,
    );

    const vote = draw({
      form: {
        outcome_label: "Your vote",
        outcomes: [approve, decline],
        groups: [],
      },
    }).root;
    expect(
      Array.from(vote.querySelectorAll("[data-slot=choice-card]")).map(
        (card) => card.textContent,
      ),
    ).toEqual(["Approve", "Decline"]);
    expect(vote.textContent).toContain("Your vote");
    expect(vote.querySelectorAll("input, textarea, [role=combobox]")).toHaveLength(
      0,
    );
  });

  it("choosing an outcome reports it, and marks the fields it requires", () => {
    const { root, onOutcome } = draw();
    click(buttonNamed(root, "Approve") as HTMLButtonElement);
    expect(onOutcome).toHaveBeenCalledWith("approve");

    const approving = draw({ outcome: "approve" }).root;
    expect(controlFor(approving, "Amount").getAttribute("aria-required")).toBe(
      "true",
    );
    expect(controlFor(approving, "Applicant").getAttribute("aria-required")).toBe(
      "true",
    );
    expect(
      buttonNamed(approving, "Approve")?.getAttribute("aria-checked"),
    ).toBe("true");

    const declining = draw({ outcome: "decline" }).root;
    expect(controlFor(declining, "Amount").hasAttribute("aria-required")).toBe(
      false,
    );
    expect(controlFor(declining, "Applicant").getAttribute("aria-required")).toBe(
      "true",
    );
  });

  it("each field type gets its input, and a money field shows its currency", () => {
    const { root } = draw({ onUpload: vi.fn() });

    expect(controlFor(root, "Applicant").tagName).toBe("INPUT");
    expect(controlFor(root, "Notes").tagName).toBe("TEXTAREA");

    const term = controlFor(root, "Term");
    expect(term.getAttribute("inputmode")).toBe("numeric");
    expect(follows(term, wordsElement(fieldOf(root, "Term"), "years"))).toBe(
      true,
    );

    const amount = controlFor(root, "Amount");
    expect(amount.getAttribute("inputmode")).toBe("decimal");
    expect(follows(wordsElement(fieldOf(root, "Amount"), "EUR"), amount)).toBe(
      true,
    );

    expect(controlFor(root, "Signed on").getAttribute("type")).toBe("date");
    expect(controlFor(root, "Due").getAttribute("type")).toBe("datetime-local");

    expect(
      Array.from(
        fieldOf(root, "Risk").querySelectorAll("[data-slot=choice-card]"),
      ).map((card) => card.textContent),
    ).toEqual(["Low", "High"]);
    expect(controlFor(root, "Channel").getAttribute("role")).toBe("combobox");

    expect(controlFor(root, "Verified").getAttribute("role")).toBe("checkbox");
    expect(controlFor(root, "Urgent").getAttribute("role")).toBe("switch");

    expect(controlFor(root, "Income").tagName).toBe("TEXTAREA");
    expect(
      buttonNamed(fieldOf(root, "Contract"), "Upload a file for Contract"),
    ).not.toBeNull();
  });

  it("typing, choosing and checking report each value", () => {
    const { root, onChange } = draw();

    typeInto(controlFor(root, "Amount") as HTMLInputElement, "182,000.50");
    expect(onChange).toHaveBeenLastCalledWith("amount", "182,000.50");

    click(buttonNamed(fieldOf(root, "Risk"), "High") as HTMLButtonElement);
    expect(onChange).toHaveBeenLastCalledWith("risk", "high");

    click(controlFor(root, "Verified"));
    expect(onChange).toHaveBeenLastCalledWith("verified", true);

    const checked = draw({ values: { urgent: true } });
    click(controlFor(checked.root, "Urgent"));
    expect(checked.onChange).toHaveBeenLastCalledWith("urgent", false);
  });

  it("a drafted field carries the draft's note", () => {
    const note =
      "Drafted by Credit analyst agent. Nothing is sent until you submit.";
    const { root } = draw({ drafted: ["amount"], draftedNote: note });

    expect(fieldOf(root, "Amount").textContent).toContain(note);
    expect(fieldOf(root, "Applicant").textContent).not.toContain(note);
    expect(fieldOf(root, "Applicant").textContent).not.toContain("Drafted");

    const plain = draw({ drafted: ["applicant"] }).root;
    expect(fieldOf(plain, "Applicant").textContent).toContain("Drafted");
  });

  it("errors show beside their field, the form's error above it, and check messages under it", () => {
    const { root } = draw({
      errors: {
        fields: { amount: "must be positive" },
        form: "Someone else must check this step.",
      },
    });

    const amount = fieldOf(root, "Amount");
    const alert = amount.querySelector("[role=alert]");
    expect(alert?.textContent).toBe("must be positive");
    expect(controlFor(root, "Amount").getAttribute("aria-invalid")).toBe("true");
    expect(controlFor(root, "Applicant").hasAttribute("aria-invalid")).toBe(
      false,
    );

    const formMessage = at(root, "Someone else must check this step.");
    expect(formMessage).toBeGreaterThanOrEqual(0);
    expect(formMessage).toBeLessThan(at(root, "Decision"));

    const check = at(root, "The amount must not exceed the lending limit.");
    expect(check).toBeGreaterThan(at(root, "Contract"));
  });

  it("an error for a field the form does not show goes above the form", () => {
    const { root } = draw({ errors: { fields: { reviewer: "is not on the team" } } });

    const above = root.querySelector("[data-slot=alert]");
    expect(above?.textContent).toContain("is not on the team");
    expect(at(root, "is not on the team")).toBeLessThan(at(root, "Decision"));
  });

  it("the output keeps decimals as plain strings, ints as numbers, and leaves empty inputs out", () => {
    expect(
      toOutput(loanForm, {
        applicant: "Ana Pop",
        notes: "  ",
        amount: "182,000.50",
        term_years: "1 250",
        verified: false,
        income: '[{"source":"salary"}]',
        due: "2026-10-23T09:30",
        documents: [],
        unknown: "x",
      }),
    ).toEqual({
      applicant: "Ana Pop",
      amount: "182000.50",
      term_years: 1250,
      verified: false,
      income: [{ source: "salary" }],
      due: new Date("2026-10-23T09:30").toISOString(),
    });

    const output = {
      amount: "182000.00",
      term_years: 25,
      due: "2026-10-23T09:30:00.000Z",
      income: [{ source: "salary" }],
      documents: [fileA],
      risk: "low",
      urgent: true,
    };
    expect(toOutput(loanForm, fromOutput(loanForm, { ...output, unknown: 1 }))).toEqual(
      output,
    );
  });

  it("text that is not a number, a date or JSON goes out as it was typed", () => {
    expect(
      toOutput(loanForm, {
        term_years: "about 20",
        due: "next week",
        income: "[unfinished",
      }),
    ).toEqual({ term_years: "about 20", due: "next week", income: "[unfinished" });
  });

  it("values taken back read as the form shows them", () => {
    const values = fromOutput(loanForm, {
      term_years: 25,
      due: new Date(2026, 9, 23, 9, 5).toISOString(),
      income: [{ source: "salary" }],
      contract: fileA,
      verified: false,
      unknown: 1,
    });

    expect(values).toEqual({
      term_years: "25",
      due: "2026-10-23T09:05",
      income: '[\n  {\n    "source": "salary"\n  }\n]',
      contract: fileA,
      verified: false,
    });
  });

  it("a decimal reads a comma or a dot as its decimal mark, and refuses one it could read two ways", () => {
    const { typing, sent, beside, missing } = typingInto("Amount", "amount");
    const examples: [string, string | undefined][] = [
      ["1,5", undefined],
      ["1.5", undefined],
      ["1.234,56", undefined],
      ["1,234.56", undefined],
      ["1 234,5", undefined],
      ["12", undefined],
      ["0,001", undefined],
      ["1.234.567", undefined],
      ["182,000.50", undefined],
      ["182,000", TWO_WAYS],
      ["1.234", TWO_WAYS],
      ["-1,234", TWO_WAYS],
      ["1.5.5", NOT_A_NUMBER],
      ["1,23,4", NOT_A_NUMBER],
      ["1,2.5", NOT_A_NUMBER],
      ["1.234,5.6", NOT_A_NUMBER],
      ["lots", NOT_A_NUMBER],
      ["-", NOT_A_NUMBER],
    ];
    const plain: Record<string, string> = {
      "1,5": "1.5",
      "1.5": "1.5",
      "1.234,56": "1234.56",
      "1,234.56": "1234.56",
      "1 234,5": "1234.5",
      "12": "12",
      "0,001": "0.001",
      "1.234.567": "1234567",
      "182,000.50": "182000.50",
    };
    for (const [typed, error] of examples) {
      typing(typed);
      expect(beside(), typed).toBe(error ?? null);
      expect(missing(), typed).toBe(error);
      // A refused number goes out as typed, for the server to refuse too.
      expect(sent(), typed).toBe(error ? typed : plain[typed]);
    }
  });

  it("an int takes thousands only in full groups of three, and refuses one it could read two ways", () => {
    const { typing, sent, beside, missing } = typingInto("Term", "term_years");
    const examples: [string, number | undefined, string | undefined][] = [
      ["1,234,567", 1234567, undefined],
      ["1.234.567", 1234567, undefined],
      ["1 234", 1234, undefined],
      ["1 250", 1250, undefined],
      ["12", 12, undefined],
      ["-1,234,567", -1234567, undefined],
      ["1,234", undefined, TWO_WAYS],
      ["1.234", undefined, TWO_WAYS],
      ["1.5", undefined, NOT_A_NUMBER],
      ["1,5", undefined, NOT_A_NUMBER],
      ["1,23,4", undefined, NOT_A_NUMBER],
      ["about 20", undefined, NOT_A_NUMBER],
    ];
    for (const [typed, number, error] of examples) {
      typing(typed);
      expect(beside(), typed).toBe(error ?? null);
      expect(missing(), typed).toBe(error);
      expect(sent(), typed).toBe(error ? typed : number);
    }
  });

  it("a draft's decimal with three places reads back as the number it is", () => {
    const values = fromOutput(loanForm, { amount: "4.975" });
    expect(values).toEqual({ amount: "4.9750" });
    expect(fromOutput(loanForm, { amount: 4.975 })).toEqual({ amount: "4.9750" });
    expect(missingInputs(loanForm, "approve", { ...values, applicant: "Ana Pop" })).toEqual({});
    expect(toOutput(loanForm, values)).toEqual({ amount: "4.9750" });
  });

  it("missing inputs name the outcome that requires them", () => {
    expect(missingInputs(loanForm, "approve", { amount: " " })).toEqual({
      applicant: "Required.",
      amount: "Required for Approve.",
    });
    expect(
      missingInputs(loanForm, "decline", { applicant: "Ana Pop", verified: false }),
    ).toEqual({});
  });

  it("a file input uploads and fills the field with the file's value", async () => {
    const uploaded: FileValue = {
      name: "contract.pdf",
      uri: "case-file://c1/contract.pdf",
      sha256: "cc33",
    };
    let finish: (value: FileValue) => void = () => undefined;
    const onUpload = vi.fn(
      () =>
        new Promise<FileValue>((resolve) => {
          finish = resolve;
        }),
    );
    const { root, onChange } = draw({ onUpload });
    const file = new File(["%PDF"], "contract.pdf", { type: "application/pdf" });

    await pickFile(fileInputOf(root, "Contract"), file);
    expect(onUpload).toHaveBeenCalledWith(file);
    const button = buttonNamed(
      fieldOf(root, "Contract"),
      "Upload a file for Contract",
    ) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.textContent).toBe("Uploading…");
    expect(onChange).not.toHaveBeenCalled();

    finish(uploaded);
    await settle();
    expect(onChange).toHaveBeenCalledWith("contract", uploaded);
    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe("Upload a file");

    const second: FileValue = { ...uploaded, name: "payslip-2.pdf", uri: "case-file://c1/payslip-2.pdf" };
    const list = draw({
      values: { documents: [fileA] },
      onUpload: vi.fn(async () => second),
    });
    await pickFile(
      fileInputOf(list.root, "Documents"),
      new File(["%PDF"], "payslip-2.pdf"),
    );
    expect(list.onChange).toHaveBeenCalledWith("documents", [fileA, second]);
  });

  it("a failed upload shows its message beside the field and keeps its files", async () => {
    const message =
      "Someone has this file open. Add it again once everyone has closed it.";
    const onUpload = vi
      .fn<(file: File) => Promise<FileValue>>()
      .mockRejectedValueOnce(new Error(message))
      .mockResolvedValueOnce(fileB);
    const { root, onChange } = draw({
      values: { documents: [fileA] },
      onUpload,
    });

    await pickFile(fileInputOf(root, "Documents"), new File(["x"], "payslip.pdf"));
    const documents = fieldOf(root, "Documents");
    expect(documents.textContent).toContain(message);
    expect(documents.textContent).toContain("id-card.pdf");
    expect(onChange).not.toHaveBeenCalled();

    await pickFile(fileInputOf(root, "Documents"), new File(["x"], "payslip.pdf"));
    expect(onChange).toHaveBeenCalledWith("documents", [fileA, fileB]);
    expect(fieldOf(root, "Documents").textContent).not.toContain(message);
  });

  it("a file already added can be picked, and a file's name opens it", () => {
    const onOpenFile = vi.fn();
    const { root, onChange } = draw({
      files: [fileA, fileB],
      values: { documents: [fileA] },
      onOpenFile,
    });

    const picker = pickerOf(root, "Documents") as HTMLSelectElement;
    expect(picker).not.toBeNull();
    expect(picker.options[0].textContent).toBe("Pick a file already added");
    expect(picker.options[0].value).toBe("");
    expect(
      Array.from(picker.options)
        .slice(1)
        .map((option) => [option.textContent, option.value]),
    ).toEqual([["payslip.pdf", fileB.uri]]);

    choose(picker, fileB.uri);
    expect(onChange).toHaveBeenCalledWith("documents", [fileA, fileB]);

    const contract = pickerOf(root, "Contract") as HTMLSelectElement;
    choose(contract, fileA.uri);
    expect(onChange).toHaveBeenLastCalledWith("contract", fileA);

    click(buttonNamed(fieldOf(root, "Documents"), "id-card.pdf") as HTMLButtonElement);
    expect(onOpenFile).toHaveBeenCalledWith(fileA);
  });

  it("a file can be removed from its field", () => {
    const { root, onChange } = draw({
      values: { documents: [fileA, fileB], contract: fileA },
    });

    click(
      buttonNamed(fieldOf(root, "Documents"), "Remove id-card.pdf") as HTMLButtonElement,
    );
    expect(onChange).toHaveBeenLastCalledWith("documents", [fileB]);

    click(
      buttonNamed(fieldOf(root, "Contract"), "Remove id-card.pdf") as HTMLButtonElement,
    );
    expect(onChange).toHaveBeenLastCalledWith("contract", undefined);
  });

  it("a required file field says it is required to a screen reader", () => {
    const required = draw({
      form: { ...loanForm, required: ["contract"] },
      onUpload: vi.fn(),
      files: [fileA],
    }).root;
    const contract = fieldOf(required, "Contract");
    const group = contract.querySelector("[role=group][aria-labelledby]") as Element;
    expect(describedText(group)).toContain("Required.");
    expect(
      describedText(
        buttonNamed(contract, "Upload a file for Contract") as HTMLButtonElement,
      ),
    ).toContain("Required.");
    const picker = pickerOf(required, "Contract") as HTMLSelectElement;
    expect(picker.getAttribute("aria-required")).toBe("true");
    expect(describedText(picker)).toContain("Required.");

    const optional = draw({ onUpload: vi.fn(), files: [fileA] }).root;
    const plain = fieldOf(optional, "Contract");
    expect(
      describedText(plain.querySelector("[role=group][aria-labelledby]") as Element),
    ).not.toContain("Required");
    expect(
      describedText(
        buttonNamed(plain, "Upload a file for Contract") as HTMLButtonElement,
      ),
    ).not.toContain("Required");
    expect(pickerOf(optional, "Contract")?.hasAttribute("aria-required")).toBe(false);
  });

  it("a read-only form takes no input", () => {
    const { root } = draw({
      readOnly: true,
      onUpload: vi.fn(),
      files: [fileA, fileB],
      values: { documents: [fileA] },
    });

    const controls = Array.from(
      root.querySelectorAll<HTMLElement>(
        "input, textarea, [role=checkbox], [role=switch], [role=combobox], [data-slot=choice-card]",
      ),
    );
    expect(controls.length).toBeGreaterThan(10);
    for (const control of controls) {
      expect(control.hasAttribute("disabled")).toBe(true);
    }
    expect(
      Array.from(root.querySelectorAll("button")).some((button) =>
        button.textContent?.includes("Upload a file"),
      ),
    ).toBe(false);
    expect(root.querySelector("select[aria-label^='Pick a file for']")).toBeNull();
    expect(buttonNamed(root, "Remove id-card.pdf")).toBeNull();
    expect(fieldOf(root, "Documents").textContent).toContain("id-card.pdf");
  });
});

// A consumer's step form, as types generated from a server's JSON Schemas
// declare it: every key present, and an absent value null. A console passes
// it straight to `SpecForm`, `fromOutput` and the file callbacks; if the
// kit's types drift from this shape, this file stops type-checking.
interface ConsumerStepForm {
  checks: string[];
  groups: ConsumerFormGroup[];
  key: string;
  name: string;
  outcome_label: string;
  outcomes: ConsumerFormOutcome[];
  required: string[];
  schema: { [k: string]: unknown };
  vote: { of: number; required: number } | null;
}
interface ConsumerFormGroup {
  help: string | null;
  inputs: ConsumerFormInput[];
  title: string | null;
}
interface ConsumerFormInput {
  choices: { label: string; value: string }[];
  field: string;
  file: ("one" | "list") | null;
  help: string | null;
  hint:
    | (
        | "text"
        | "textarea"
        | "number"
        | "money"
        | "date"
        | "datetime"
        | "select"
        | "radio"
        | "checkbox"
        | "switch"
        | "file"
      )
    | null;
  label: string;
  placeholder: string | null;
  type:
    | "string"
    | "int"
    | "decimal"
    | "bool"
    | "date"
    | "datetime"
    | "choice"
    | "file"
    | "list"
    | "object";
  unit: string | null;
}
interface ConsumerFormOutcome {
  key: string;
  label: string;
  required: string[];
}
interface ConsumerFileValue {
  name: string;
  sha256: string;
  uri: string;
}

describe("a consumer's step form", () => {
  const plainInput: Omit<ConsumerFormInput, "field" | "label" | "type"> = {
    choices: [],
    file: null,
    help: null,
    hint: null,
    placeholder: null,
    unit: null,
  };

  const consumerForm: ConsumerStepForm = {
    key: "assess",
    name: "Assess",
    outcome_label: "Decision",
    outcomes: [{ key: "approve", label: "Approve", required: ["amount"] }],
    required: [],
    checks: [],
    schema: { type: "object" },
    vote: null,
    groups: [
      {
        title: null,
        help: null,
        inputs: [
          { ...plainInput, field: "amount", label: "Amount", type: "decimal" },
          {
            ...plainInput,
            field: "evidence",
            label: "Evidence",
            type: "file",
            hint: "file",
            file: "one",
          },
        ],
      },
    ],
  };

  it("draws with every absent value null, and takes its draft and files as they are", async () => {
    const draft: { output: { [key: string]: unknown } } = {
      output: { amount: "1200.00" },
    };
    const consumerFiles: ConsumerFileValue[] = [fileA];
    const opened: ConsumerFileValue[] = [];
    const onChange = vi.fn();
    const root = render(
      <SpecForm
        form={consumerForm}
        values={fromOutput(consumerForm, draft.output)}
        drafted={Object.keys(draft.output)}
        onChange={onChange}
        outcome="approve"
        files={consumerFiles}
        onUpload={async (file: File): Promise<ConsumerFileValue> => ({
          name: file.name,
          uri: `case-file://c1/${file.name}`,
          sha256: "dd44",
        })}
        onOpenFile={(value: ConsumerFileValue) => opened.push(value)}
      />,
    );

    expect(Array.from(root.querySelectorAll("legend")).map((l) => l.textContent)).toEqual([
      "Decision",
    ]);
    const amount = controlFor(root, "Amount") as HTMLInputElement;
    expect(amount.value).toBe("1200.00");
    expect(amount.getAttribute("aria-required")).toBe("true");
    expect(amount.closest("[data-slot=input-group]")).toBeNull();
    expect(fieldOf(root, "Amount").textContent).toContain("Drafted");

    await pickFile(fileInputOf(root, "Evidence"), new File(["x"], "scan.pdf"));
    expect(onChange).toHaveBeenCalledWith("evidence", {
      name: "scan.pdf",
      uri: "case-file://c1/scan.pdf",
      sha256: "dd44",
    });
  });
});
