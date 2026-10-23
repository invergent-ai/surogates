// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A form's values going out and coming back. A field holds what a person
// typed; the output holds what a server takes: numbers written the way
// people write them ("182,000.50", "1 250") made plain, a local date and
// time made an instant, JSON text parsed. Text that does not read as its
// type goes out as typed, for the server to reject in its own words.
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

/** A whole number as people write it, without its spaces and thousands commas. */
function plainNumber(text: string): string {
  return text.replace(/[\s,]/g, "");
}

/**
 * A decimal as people write it, made plain digits with a dot. Either a
 * comma or a dot may mark the decimals: whichever comes last does, and
 * every other comma, dot or space separates thousands ("1.234,56" and
 * "1,234.56" both read 1234.56). Null when what is left is not a number.
 */
function plainDecimal(text: string): string | null {
  const compact = text.replace(/\s/g, "");
  const negative = compact.startsWith("-");
  const digits = negative ? compact.slice(1) : compact;
  const separator = Math.max(digits.lastIndexOf(","), digits.lastIndexOf("."));
  const whole =
    separator < 0 ? digits : digits.slice(0, separator).replace(/[.,]/g, "");
  const fraction = separator < 0 ? "" : digits.slice(separator + 1);
  if (!/^\d*$/.test(whole) || !/^\d*$/.test(fraction)) return null;
  if (whole === "" && fraction === "") return null;
  const plain = (whole || "0") + (fraction ? `.${fraction}` : "");
  return negative ? `-${plain}` : plain;
}

/** A decimal field's text that does not read as a number. */
function isNotADecimal(input: FormSpecInput, value: FormValue): boolean {
  return (
    input.type === "decimal" &&
    !isFileInput(input) &&
    typeof value === "string" &&
    plainDecimal(value) === null
  );
}

function outputOf(input: FormSpecInput, value: FormValue): unknown {
  if (isFileInput(input) || typeof value !== "string") return value;
  switch (input.type) {
    case "decimal":
      return plainDecimal(value) ?? value;
    case "int": {
      const plain = plainNumber(value);
      return /^-?\d+$/.test(plain) ? Number(plain) : value;
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
  // A number reads as its decimal text: an int always, a decimal sent bare.
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
 * decimal that does not read as a number.
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
      if (isNotADecimal(input, value)) missing[input.field] = "Not a number.";
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
