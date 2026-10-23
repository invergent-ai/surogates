// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// One control per field type and hint, and the file input. Every control
// holds what a person sees: numbers, dates and JSON stay text here, and
// `values.ts` makes them what a server takes.
"use client";

import { FileIcon, UploadIcon, XIcon } from "lucide-react";
import { type ChangeEvent, useRef, useState } from "react";

import { Button } from "../button";
import { Checkbox } from "../checkbox";
import { ChoiceCard } from "../choice-card";
import { CONTROL_FIELD, CONTROL_OPTION } from "../control-styles";
import { FieldError } from "../field";
import { Input } from "../input";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
  InputGroupText,
} from "../input-group";
import { cn } from "../lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../select";
import { Switch } from "../switch";
import { TextLink } from "../text-link";
import { Textarea } from "../textarea";
import type { FileValue, FormSpecInput, FormValue } from "./types";
import { filesIn, isFileInput } from "./values";

/** Which control draws a field. The first rule that holds decides. */
export type ControlKind =
  | "file"
  | "cards"
  | "select"
  | "switch"
  | "checkbox"
  | "number"
  | "date"
  | "datetime"
  | "textarea"
  | "text"
  | "json";

export function controlKind(input: FormSpecInput): ControlKind {
  if (isFileInput(input)) return "file";
  switch (input.type) {
    case "choice":
      return input.hint === "radio" ? "cards" : "select";
    case "bool":
      return input.hint === "switch" ? "switch" : "checkbox";
    case "int":
    case "decimal":
      return "number";
    case "date":
      return "date";
    case "datetime":
      return "datetime";
    case "string":
      return input.hint === "textarea" ? "textarea" : "text";
    default:
      return "json";
  }
}

/**
 * A set of cards or files is named by `aria-labelledby` on its group; any
 * other control is a single element a `<label for>` can name.
 */
export function isGroupControl(kind: ControlKind): boolean {
  return kind === "file" || kind === "cards";
}

/** A checkbox or a switch sits before its label, on one line with it. */
export function isInlineControl(kind: ControlKind): boolean {
  return kind === "checkbox" || kind === "switch";
}

/** A currency: a three-letter code, or any unit on a money field. */
function isMoney(input: FormSpecInput): boolean {
  return input.hint === "money" || /^[A-Z]{3}$/.test(input.unit ?? "");
}

export interface CardChoicesProps {
  options: readonly { value: string; label: string }[];
  value: string | null | undefined;
  onPick: (value: string) => void;
  /** The id of the words that name the set. */
  labelledBy: string;
  id?: string;
  disabled?: boolean;
  required?: boolean;
  invalid?: boolean;
  describedBy?: string;
}

/** One small card per option, exactly one of them picked: a radio group. */
export function CardChoices({
  options,
  value,
  onPick,
  labelledBy,
  id,
  disabled,
  required,
  invalid,
  describedBy,
}: CardChoicesProps) {
  return (
    <div
      role="radiogroup"
      id={id}
      aria-labelledby={labelledBy}
      aria-required={required || undefined}
      aria-invalid={invalid || undefined}
      aria-describedby={describedBy}
      className="grid grid-cols-1 gap-2 sm:grid-cols-[repeat(auto-fill,minmax(9rem,1fr))]"
    >
      {options.map((option) => (
        <ChoiceCard
          key={option.value}
          size="sm"
          role="radio"
          aria-pressed={undefined}
          aria-checked={value === option.value}
          selected={value === option.value}
          title={option.label}
          disabled={disabled}
          onClick={() => onPick(option.value)}
        />
      ))}
    </div>
  );
}

export interface SpecInputProps {
  input: FormSpecInput;
  /** The control's id, or its group's. */
  id: string;
  /** The id of the field's label. */
  labelId: string;
  value: FormValue | undefined;
  onChange: (value: FormValue | undefined) => void;
  required: boolean;
  invalid: boolean;
  describedBy?: string;
  readOnly?: boolean;
  onUpload?: (file: File) => Promise<FileValue>;
  files?: readonly FileValue[];
  onOpenFile?: (value: FileValue) => void;
}

/** The control a field's type and hint call for. */
export function SpecInput(props: SpecInputProps) {
  const { input, id, labelId, value, onChange, required, invalid, describedBy, readOnly } =
    props;
  const kind = controlKind(input);
  const text = typeof value === "string" ? value : "";
  const aria = {
    "aria-required": required || undefined,
    "aria-invalid": invalid || undefined,
    "aria-describedby": describedBy,
  };
  const typed = (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    onChange(event.target.value);

  switch (kind) {
    case "file":
      return <FileInput {...props} />;
    case "cards":
      return (
        <CardChoices
          id={id}
          labelledBy={labelId}
          options={input.choices ?? []}
          value={typeof value === "string" ? value : null}
          onPick={onChange}
          disabled={readOnly}
          required={required}
          invalid={invalid}
          describedBy={describedBy}
        />
      );
    case "select":
      return (
        <Select value={text} onValueChange={onChange} disabled={readOnly}>
          <SelectTrigger id={id} className="w-full" {...aria}>
            <SelectValue placeholder={input.placeholder || "Pick one"} />
          </SelectTrigger>
          <SelectContent>
            {(input.choices ?? []).map((choice) => (
              <SelectItem key={choice.value} value={choice.value}>
                {choice.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    case "switch":
      return (
        <Switch
          id={id}
          checked={value === true}
          onCheckedChange={(checked) => onChange(checked === true)}
          disabled={readOnly}
          {...aria}
        />
      );
    case "checkbox":
      return (
        <Checkbox
          id={id}
          checked={value === true}
          onCheckedChange={(checked) => onChange(checked === true)}
          disabled={readOnly}
          {...aria}
        />
      );
    case "number": {
      const control = {
        id,
        inputMode: input.type === "int" ? ("numeric" as const) : ("decimal" as const),
        value: text,
        onChange: typed,
        placeholder: input.placeholder ?? undefined,
        disabled: readOnly,
        ...aria,
      };
      const unit = input.unit;
      if (!unit) return <Input {...control} />;
      const money = isMoney(input);
      const addon = (
        <InputGroupAddon align={money ? "inline-start" : "inline-end"}>
          <InputGroupText>{unit}</InputGroupText>
        </InputGroupAddon>
      );
      return (
        <InputGroup data-disabled={readOnly || undefined}>
          {money && addon}
          <InputGroupInput {...control} />
          {!money && addon}
        </InputGroup>
      );
    }
    case "date":
    case "datetime":
      return (
        <Input
          id={id}
          type={kind === "date" ? "date" : "datetime-local"}
          value={text}
          onChange={typed}
          disabled={readOnly}
          {...aria}
        />
      );
    case "textarea":
    case "json":
      return (
        <Textarea
          id={id}
          value={text}
          onChange={typed}
          placeholder={input.placeholder ?? undefined}
          disabled={readOnly}
          spellCheck={kind === "json" ? false : undefined}
          className={cn(kind === "json" && "font-mono text-[12.5px]")}
          {...aria}
        />
      );
    default:
      return (
        <Input
          id={id}
          value={text}
          onChange={typed}
          placeholder={input.placeholder ?? undefined}
          disabled={readOnly}
          {...aria}
        />
      );
  }
}

function messageOf(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return "The file could not be uploaded.";
}

/**
 * The files a field holds, each opened by its name and removed by its ×; an
 * upload; and a pick among the files already added elsewhere.
 */
function FileInput({
  input,
  id,
  labelId,
  value,
  onChange,
  describedBy,
  readOnly,
  onUpload,
  files,
  onOpenFile,
}: SpecInputProps) {
  const many = input.file === "list";
  const held = filesIn(value);
  // An upload ends after renders the field may have had in between: append
  // to the files it holds then, not to those it held when the upload began.
  const latest = useRef(held);
  latest.current = held;
  const chooser = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const failureId = `${id}-failure`;
  const pickable = (files ?? []).filter(
    (file) => !held.some((h) => h.uri === file.uri),
  );

  const add = (file: FileValue) =>
    onChange(many ? [...latest.current, file] : file);

  const remove = (index: number) =>
    onChange(many ? held.filter((_, i) => i !== index) : undefined);

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    // Cleared so that choosing the same file again still reads as a change.
    event.currentTarget.value = "";
    if (!file || !onUpload) return;
    setUploading(true);
    try {
      const uploaded = await onUpload(file);
      setFailure(null);
      add(uploaded);
    } catch (error) {
      setFailure(messageOf(error));
    } finally {
      setUploading(false);
    }
  }

  function pick(event: ChangeEvent<HTMLSelectElement>) {
    const file = pickable.find((f) => f.uri === event.target.value);
    if (!file) return;
    setFailure(null);
    add(file);
  }

  return (
    <div
      role="group"
      id={id}
      aria-labelledby={labelId}
      aria-describedby={describedBy}
      className="flex flex-col gap-2"
    >
      {held.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {held.map((file, index) => (
            <li
              key={`${file.uri}-${index}`}
              className="flex min-h-10 items-center gap-2.5 rounded-[10px] border border-foreground/10 bg-foreground/[0.02] px-3 py-1.5"
            >
              <FileIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              {onOpenFile ? (
                <TextLink asChild size="sm" className="min-w-0">
                  <button type="button" onClick={() => onOpenFile(file)}>
                    <span className="truncate">{file.name}</span>
                  </button>
                </TextLink>
              ) : (
                <span className="min-w-0 truncate text-[13px] text-foreground">
                  {file.name}
                </span>
              )}
              {!readOnly && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className="ml-auto text-muted-foreground"
                  aria-label={`Remove ${file.name}`}
                  onClick={() => remove(index)}
                >
                  <XIcon />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {!readOnly && (onUpload || pickable.length > 0) && (
        <div className="flex flex-wrap items-center gap-2">
          {onUpload && (
            <>
              <input
                ref={chooser}
                type="file"
                hidden
                tabIndex={-1}
                onChange={upload}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={uploading}
                aria-busy={uploading || undefined}
                aria-label={`Upload a file for ${input.label}`}
                aria-describedby={failure ? failureId : undefined}
                onClick={() => chooser.current?.click()}
              >
                <UploadIcon />
                {uploading ? "Uploading…" : "Upload a file"}
              </Button>
            </>
          )}
          {pickable.length > 0 && (
            <select
              aria-label={`Pick a file for ${input.label}`}
              className={cn(CONTROL_FIELD, "sm:w-auto sm:min-w-56")}
              value=""
              onChange={pick}
            >
              <option value="" className={CONTROL_OPTION}>
                Pick a file already added
              </option>
              {pickable.map((file) => (
                <option key={file.uri} value={file.uri} className={CONTROL_OPTION}>
                  {file.name}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
      {failure && <FieldError id={failureId}>{failure}</FieldError>}
    </div>
  );
}
