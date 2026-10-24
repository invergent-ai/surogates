// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A value shown to copy: a read-only field named by its label, with Copy. A
// secret is masked until Show, and only the field holds it: no title, no
// data attribute, no log. When the browser refuses the clipboard, the field
// says how to copy it by hand. Ported from Studio's one-time password. The
// app decides how long the value lives: a key shown once is gone when the
// app stops passing it.
"use client";

import { type ReactNode, useId, useState } from "react";

import { Button } from "./button";
import { Field, FieldDescription, FieldLabel } from "./field";
import { Input } from "./input";

const REFUSED = "The browser did not let this be copied. Select it and copy it.";

/** What the field shows of the value it was given. */
type Shown = {
  value: string;
  revealed: boolean;
  copied: boolean;
  refused: boolean;
};

function fresh(value: string): Shown {
  return { value, revealed: false, copied: false, refused: false };
}

export function CopyField({
  value,
  label,
  secret = false,
  description,
  className,
}: {
  value: string;
  label: string;
  /** Masks the value until a person shows it. */
  secret?: boolean;
  description?: ReactNode;
  className?: string;
}) {
  const id = useId();
  const [shown, setShown] = useState(() => fresh(value));
  // A new value starts masked, uncopied and without a refusal.
  if (shown.value !== value) setShown(fresh(value));

  const copy = async () => {
    // A copy that ends after the value changed leaves the new one alone.
    const settle = (copied: boolean) =>
      setShown((now) =>
        now.value === value ? { ...now, copied, refused: !copied } : now,
      );
    try {
      // Inside the try: plain HTTP and some embedded browsers have no
      // clipboard at all.
      await navigator.clipboard.writeText(value);
      settle(true);
    } catch {
      settle(false);
    }
  };

  return (
    <Field className={className}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          readOnly
          value={value}
          type={secret && !shown.revealed ? "password" : "text"}
          autoComplete={secret ? "off" : undefined}
          className="font-mono"
        />
        {secret && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() =>
              setShown((now) => ({ ...now, revealed: !now.revealed }))
            }
          >
            {shown.revealed ? "Hide" : "Show"}
          </Button>
        )}
        <Button type="button" variant="outline" size="sm" onClick={copy}>
          {shown.copied ? "Copied" : "Copy"}
        </Button>
      </div>
      {description && <FieldDescription>{description}</FieldDescription>}
      {/* Always there, so a screen reader announces the words when they
          arrive; out of the layout while empty. */}
      <FieldDescription
        role="status"
        className={shown.refused ? undefined : "sr-only"}
      >
        {shown.refused ? REFUSED : null}
      </FieldDescription>
    </Field>
  );
}
