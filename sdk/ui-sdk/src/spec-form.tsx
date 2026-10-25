// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A form drawn from a spec: its outcome first, then its groups of fields,
// each field drawn by its type and hint, then the notes on what the server
// checks. It is not a <form>: the app owns submitting, and decides what an
// error says and where (`errors`), what the form holds (`values`) and how a
// file gets uploaded (`onUpload`).
"use client";

import { CircleAlertIcon, InfoIcon } from "lucide-react";
import { useId } from "react";

import { Alert, AlertDescription } from "./alert";
import { Chip } from "./chip";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "./field";
import {
  CardChoices,
  controlKind,
  isGroupControl,
  isInlineControl,
  SpecInput,
} from "./spec-form/inputs";
import type {
  FileValue,
  FormSpec,
  FormSpecInput,
  FormValue,
  FormValues,
  FormErrors,
} from "./spec-form/types";
import { inputsOf } from "./spec-form/values";

export type {
  FileValue,
  FormErrors,
  FormFieldHint,
  FormFieldType,
  FormSpec,
  FormSpecChoice,
  FormSpecGroup,
  FormSpecInput,
  FormSpecOutcome,
  FormValue,
  FormValues,
} from "./spec-form/types";
export { fromOutput, missingInputs, toOutput } from "./spec-form/values";

export interface SpecFormProps {
  form: FormSpec;
  values: FormValues;
  onChange: (field: string, value: FormValue | undefined) => void;
  /** The outcome's key, once one is chosen. */
  outcome?: string | null;
  onOutcome?: (key: string) => void;
  errors?: FormErrors;
  /** The fields a draft filled, each marked with `draftedNote`. */
  drafted?: readonly string[];
  draftedNote?: string;
  /** A note per drafted field, which wins over `draftedNote`; a field with a note here is marked even when `drafted` leaves it out. */
  draftedNotes?: Record<string, string>;
  /** Shows the values with every control disabled, and no upload, pick or remove. */
  readOnly?: boolean;
  /** Uploads a file and resolves the value a file field then holds. */
  onUpload?: (file: File) => Promise<FileValue>;
  /** The files already added elsewhere, which a file field may pick. */
  files?: readonly FileValue[];
  /** Opens a file a field holds, from its name. */
  onOpenFile?: (value: FileValue) => void;
}

interface SpecFieldProps
  extends Pick<
    SpecFormProps,
    "readOnly" | "onUpload" | "files" | "onOpenFile"
  > {
  input: FormSpecInput;
  id: string;
  value: FormValue | undefined;
  onChange: (value: FormValue | undefined) => void;
  required: boolean;
  note?: string;
  error?: string;
}

/** A field: its label, a draft's note, the control, its help and its error. */
function SpecField({
  input,
  id,
  value,
  onChange,
  required,
  note,
  error,
  ...rest
}: SpecFieldProps) {
  const kind = controlKind(input);
  const labelId = `${id}-label`;
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const describedBy =
    [input.help ? helpId : null, error ? errorId : null]
      .filter(Boolean)
      .join(" ") || undefined;

  const label = (
    <FieldLabel
      id={labelId}
      htmlFor={isGroupControl(kind) ? undefined : id}
      className="gap-1"
    >
      {input.label}
      {required && (
        <span aria-hidden="true" className="text-destructive">
          *
        </span>
      )}
    </FieldLabel>
  );
  const drafted = note ? (
    <Chip tone="info" size="sm" className="whitespace-normal">
      {note}
    </Chip>
  ) : null;
  const control = (
    <SpecInput
      input={input}
      id={id}
      labelId={labelId}
      value={value}
      onChange={onChange}
      required={required}
      invalid={Boolean(error)}
      describedBy={describedBy}
      {...rest}
    />
  );

  return (
    <Field data-invalid={error ? true : undefined} className="gap-2">
      {isInlineControl(kind) ? (
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
          {control}
          {label}
          {drafted}
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {label}
            {drafted}
          </div>
          {control}
        </>
      )}
      {input.help && <FieldDescription id={helpId}>{input.help}</FieldDescription>}
      {error && <FieldError id={errorId}>{error}</FieldError>}
    </Field>
  );
}

export function SpecForm({
  form,
  values,
  onChange,
  outcome,
  onOutcome,
  errors,
  drafted,
  draftedNote = "Drafted",
  draftedNotes,
  readOnly,
  onUpload,
  files,
  onOpenFile,
}: SpecFormProps) {
  const baseId = useId();
  const outcomes = form.outcomes ?? [];
  const chosen = outcomes.find((o) => o.key === outcome);
  const required = new Set([
    ...(form.required ?? []),
    ...(chosen?.required ?? []),
  ]);
  const fieldNotes = new Map(Object.entries(draftedNotes ?? {}));
  const draftedFields = new Set([...(drafted ?? []), ...fieldNotes.keys()]);
  const fieldErrors = errors?.fields ?? {};
  const shown = new Set(inputsOf(form).map((input) => input.field));
  // An error naming a field this form does not show would have nowhere to
  // stand, so it joins the form's own, above the form.
  const above = [
    ...(errors?.form ? [errors.form] : []),
    ...Object.entries(fieldErrors)
      .filter(([field]) => !shown.has(field))
      .map(([field, message]) => `${field}: ${message}`),
  ];
  const outcomeLegendId = `${baseId}-outcome`;

  return (
    <div data-slot="spec-form" className="flex flex-col gap-8">
      {above.length > 0 && (
        <Alert variant="destructive">
          <CircleAlertIcon />
          <AlertDescription>
            {above.map((message, index) => (
              <p key={index}>{message}</p>
            ))}
          </AlertDescription>
        </Alert>
      )}
      {outcomes.length > 0 && (
        <FieldSet>
          <FieldLegend id={outcomeLegendId}>
            {form.outcome_label?.trim() || "Outcome"}
          </FieldLegend>
          <CardChoices
            labelledBy={outcomeLegendId}
            options={outcomes.map((o) => ({ value: o.key, label: o.label }))}
            value={outcome}
            onPick={(key) => onOutcome?.(key)}
            disabled={readOnly}
          />
        </FieldSet>
      )}
      {(form.groups ?? []).map((group, groupIndex) => (
        <FieldSet key={groupIndex}>
          {group.title && <FieldLegend>{group.title}</FieldLegend>}
          {group.help && <FieldDescription>{group.help}</FieldDescription>}
          {(group.inputs ?? []).map((input, inputIndex) => (
            <SpecField
              key={`${input.field}-${inputIndex}`}
              input={input}
              id={`${baseId}-${groupIndex}-${inputIndex}`}
              value={values[input.field]}
              onChange={(value) => onChange(input.field, value)}
              required={required.has(input.field)}
              note={
                draftedFields.has(input.field)
                  ? (fieldNotes.get(input.field) ?? draftedNote)
                  : undefined
              }
              error={fieldErrors[input.field]}
              readOnly={readOnly}
              onUpload={onUpload}
              files={files}
              onOpenFile={onOpenFile}
            />
          ))}
        </FieldSet>
      ))}
      {(form.checks ?? []).length > 0 && (
        <ul data-slot="spec-form-checks" className="flex flex-col gap-1.5">
          {(form.checks ?? []).map((check, index) => (
            <li
              key={index}
              className="flex items-start gap-2 text-[12.5px] leading-relaxed text-muted-foreground"
            >
              <InfoIcon aria-hidden className="mt-[3px] size-3.5 shrink-0" />
              <span>{check}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
