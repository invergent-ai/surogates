// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A form's values going out and coming back. A field holds what a person
// typed; the output holds what a server takes: numbers written the way
// people write them ("182,000.50", "1 234,5") made plain, a local date and
// time made an instant, JSON text parsed. Text that does not read as its
// type goes out as typed, for the server to reject in its own words; a
// number that could be read two ways ("182,000") is never guessed at, and
// `missingInputs` names it before the form is sent.
import type {
  FileValue,
  FormSpec,
  FormSpecInput,
  FormValue,
  FormValues,
} from "./types";

/** Every input the form shows, in the order it shows them. */
export function inputsOf(form: FormSpec): FormSpecInput[] {
  return (form.groups ?? []).flatMap((group) => [...(group.inputs ?? [])]);
}

/** A field that holds a file, or several. */
export function isFileInput(input: FormSpecInput): boolean {
  return Boolean(input.file) || input.type === "file";
}

/** A list or an object, shown as JSON text. */
export function isJsonInput(input: FormSpecInput): boolean {
  return (input.type === "list" || input.type === "object") && !isFileInput(input);
}

export function isFileValue(value: unknown): value is FileValue {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    typeof (value as FileValue).uri === "string" &&
    typeof (value as FileValue).name === "string"
  );
}

/** The files a value holds: none, one, or a list. */
export function filesIn(value: FormValue | undefined): FileValue[] {
  if (Array.isArray(value)) return value.filter(isFileValue);
  return isFileValue(value) ? [value] : [];
}

/** Nothing there: no value, blank text or an empty list. `false` is a value. */
export function isEmptyValue(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (typeof value === "string") return value.trim() === "";
  return Array.isArray(value) && value.length === 0;
}

/** What a number field's text reads as: plain digits, or why it is refused. */
type NumberReading =
  | { plain: string; problem?: undefined }
  | { plain?: undefined; problem: string };

const TWO_WAYS = "Write the number without thousands separators.";
const NOT_A_NUMBER = "Not a number.";

function refused(problem: string): NumberReading {
  return { problem };
}

/**
 * A number's sign, its digit groups and the separators between them, a run
 * of spaces as one " ". Null when the text holds anything else, or a
 * separator without digits on both sides; a leading mark (".5") reads "0.5".
 */
function numberParts(
  text: string,
): { sign: string; groups: string[]; separators: string[] } | null {
  let rest = text.trim().replace(/\s+/g, " ");
  const sign = rest.startsWith("-") ? "-" : "";
  rest = rest.slice(sign.length);
  if (/^[.,]/.test(rest)) rest = `0${rest}`;
  if (!/^\d+([., ]\d+)*$/.test(rest)) return null;
  return {
    sign,
    groups: rest.split(/[., ]/),
    separators: rest.match(/[., ]/g) ?? [],
  };
}

/** Every group after the first holds three digits: thousands, written out. */
function inThousands(groups: readonly string[]): boolean {
  return groups.slice(1).every((group) => group.length === 3);
}

/** A first group that could open a number in thousands: "0,001" cannot. */
function opensThousands(group: string): boolean {
  return !group.startsWith("0");
}

/**
 * A decimal as people write it, as plain digits with a dot. The comma or
 * dot that comes last, once, marks the decimals ("1.234,56" and "1,234.56"
 * both read 1234.56). A comma or dot that repeats, and every space,
 * separates thousands, in groups of three. A lone comma or dot before
 * exactly three digits could be either ("182,000" is 182000 or 182), so it
 * is refused rather than guessed.
 */
function readDecimal(text: string): NumberReading {
  const parts = numberParts(text);
  if (!parts) return refused(NOT_A_NUMBER);
  const { sign, groups, separators } = parts;
  const marks = separators.filter((s) => s !== " ");
  const last = marks[marks.length - 1];
  if (last === undefined || marks.filter((s) => s === last).length > 1) {
    // No decimal mark: every separator is a thousands one, of one kind.
    if (new Set(marks).size > 1 || !inThousands(groups)) {
      return refused(NOT_A_NUMBER);
    }
    return { plain: sign + groups.join("") };
  }
  // Nothing but digits may follow the decimal mark.
  if (separators.lastIndexOf(last) !== separators.length - 1) {
    return refused(NOT_A_NUMBER);
  }
  const whole = groups.slice(0, -1);
  const fraction = groups[groups.length - 1];
  if (!inThousands(whole)) return refused(NOT_A_NUMBER);
  if (
    separators.length === 1 &&
    fraction.length === 3 &&
    opensThousands(whole[0])
  ) {
    return refused(TWO_WAYS);
  }
  return { plain: `${sign}${whole.join("")}.${fraction}` };
}

/**
 * A whole number as people write it, as plain digits. Spaces separate
 * thousands, and so does a comma or a dot that repeats, in groups of three.
 * A lone comma or dot before exactly three digits could be thousands or
 * decimals ("1,234"), so it is refused; before any other count of digits it
 * marks decimals, which a whole number does not have.
 */
function readInt(text: string): NumberReading {
  const parts = numberParts(text);
  if (!parts) return refused(NOT_A_NUMBER);
  const { sign, groups, separators } = parts;
  const marks = separators.filter((s) => s !== " ");
  if (new Set(marks).size > 1 || !inThousands(groups)) {
    return refused(NOT_A_NUMBER);
  }
  if (marks.length === 1) {
    return refused(opensThousands(groups[0]) ? TWO_WAYS : NOT_A_NUMBER);
  }
  return { plain: sign + groups.join("") };
}

/** Why a decimal field's text cannot go out as a number; null when it can. */
function isNotADecimal(input: FormSpecInput, value: FormValue): string | null {
  if (input.type !== "decimal" || isFileInput(input)) return null;
  return typeof value === "string" ? (readDecimal(value).problem ?? null) : null;
}

/** Why an int field's text cannot go out as a whole number; null when it can. */
function isNotAnInt(input: FormSpecInput, value: FormValue): string | null {
  if (input.type !== "int" || isFileInput(input)) return null;
  return typeof value === "string" ? (readInt(value).problem ?? null) : null;
}

function outputOf(input: FormSpecInput, value: FormValue): unknown {
  if (isFileInput(input) || typeof value !== "string") return value;
  switch (input.type) {
    case "decimal":
      return readDecimal(value).plain ?? value;
    case "int": {
      const { plain } = readInt(value);
      return plain === undefined ? value : Number(plain);
    }
    case "datetime": {
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toISOString();
    }
    case "list":
    case "object":
      try {
        return JSON.parse(value);
      } catch {
        return value;
      }
    default:
      return value;
  }
}

/**
 * What the form sends: one entry per input it shows that holds a value,
 * each as its type goes out. Keys the form does not show are left out.
 */
export function toOutput(
  form: FormSpec,
  values: FormValues,
): Record<string, unknown> {
  const output: Record<string, unknown> = {};
  for (const input of inputsOf(form)) {
    const value = values[input.field];
    if (value === undefined || isEmptyValue(value)) continue;
    output[input.field] = outputOf(input, value);
  }
  return output;
}

function pad(n: number, width = 2): string {
  return String(n).padStart(width, "0");
}

/** An instant as a `datetime-local` input reads it: `YYYY-MM-DDTHH:mm`, here. */
function localDateTime(date: Date): string {
  // Seconds are dropped on purpose: no form drawn from a spec asks for them.
  return (
    `${pad(date.getFullYear(), 4)}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function valueOf(input: FormSpecInput, value: unknown): FormValue {
  if (isFileInput(input)) return value as FormValue;
  if (isJsonInput(input)) return JSON.stringify(value, null, 2);
  if (input.type === "datetime" && typeof value === "string") {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : localDateTime(date);
  }
  if (
    input.type === "decimal" &&
    (typeof value === "string" || typeof value === "number")
  ) {
    // A server's "4.975" means 4.975. Read back as typed, it could be read
    // two ways and would be refused, so it gains a trailing 0.
    const text = String(value);
    return readDecimal(text).problem === TWO_WAYS ? `${text}0` : text;
  }
  // An int reads as its decimal text.
  if (typeof value === "number") return String(value);
  return value as FormValue;
}

/**
 * The values a form holds for an output it sent or a draft of one: the
 * reverse of `toOutput`, for the inputs the form shows.
 */
export function fromOutput(
  form: FormSpec,
  output: Readonly<Record<string, unknown>>,
): FormValues {
  const values: Record<string, FormValue> = {};
  for (const input of inputsOf(form)) {
    const value = output[input.field];
    if (value === undefined || value === null) continue;
    values[input.field] = valueOf(input, value);
  }
  return values;
}

/**
 * The inputs that cannot go out yet, each with the words to show beside it:
 * those still empty that the form, or the outcome chosen, requires, and a
 * number that is not one, or that could be read two ways.
 */
export function missingInputs(
  form: FormSpec,
  outcome: string | null | undefined,
  values: FormValues,
): Record<string, string> {
  const chosen = (form.outcomes ?? []).find((o) => o.key === outcome);
  const missing: Record<string, string> = {};
  for (const input of inputsOf(form)) {
    const value = values[input.field];
    if (value !== undefined && !isEmptyValue(value)) {
      const refusal = isNotADecimal(input, value) ?? isNotAnInt(input, value);
      if (refusal) missing[input.field] = refusal;
      continue;
    }
    if (form.required?.includes(input.field)) {
      missing[input.field] = "Required.";
    } else if (chosen?.required?.includes(input.field)) {
      missing[input.field] = `Required for ${chosen.label}.`;
    }
  }
  return missing;
}
