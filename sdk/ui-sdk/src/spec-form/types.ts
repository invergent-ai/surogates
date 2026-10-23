// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The spec a form is drawn from, and the values it holds. The shapes are
// structural: a server's generated form type, where every absent value is
// null, passes as it is.

/** What a field holds once sent. */
export type FormFieldType =
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

/** How a field would like to be drawn; the type decides where the two disagree. */
export type FormFieldHint =
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
  | "file";

export interface FormSpecChoice {
  value: string;
  label: string;
}

export interface FormSpecInput {
  field: string;
  label: string;
  type: FormFieldType;
  hint?: FormFieldHint | null;
  /** Set when the field holds a file (`one`) or several (`list`). */
  file?: "one" | "list" | null;
  /** A currency code shows before the value; any other unit after it. */
  unit?: string | null;
  choices?: readonly FormSpecChoice[];
  help?: string | null;
  placeholder?: string | null;
}

export interface FormSpecGroup {
  title?: string | null;
  help?: string | null;
  inputs: readonly FormSpecInput[];
}

export interface FormSpecOutcome {
  key: string;
  label: string;
  /** The fields this outcome needs on top of the form's own. */
  required?: readonly string[];
}

export interface FormSpec {
  /** The outcome's legend; `Outcome` when absent. */
  outcome_label?: string | null;
  /** None for a start form; a vote's form has outcomes and no groups. */
  outcomes?: readonly FormSpecOutcome[];
  required?: readonly string[];
  /** What the server checks on submit, shown as notes under the form. */
  checks?: readonly string[];
  groups: readonly FormSpecGroup[];
}

/** A file a field holds: its name, its address and its content's hash. */
export interface FileValue {
  name: string;
  uri: string;
  sha256: string;
}

/**
 * A field's value as the form holds it: text for every typed field (numbers
 * and JSON included), a boolean, or a file or a list of files.
 */
export type FormValue = string | boolean | FileValue | readonly FileValue[];

export type FormValues = Readonly<Record<string, FormValue | undefined>>;

/** Messages by the field they name, and one for the form as a whole. */
export interface FormErrors {
  fields?: Readonly<Record<string, string>>;
  form?: string | null;
}
