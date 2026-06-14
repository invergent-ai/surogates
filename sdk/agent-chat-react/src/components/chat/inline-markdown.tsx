// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Lightweight INLINE markdown renderer for compact, single-line chat
// labels — iteration summaries and narration whispers — where the
// block-level ``MessageResponse``/Streamdown renderer doesn't fit
// (those surfaces live inside ``truncate`` one-liners and must stay
// inline). Handles the inline tokens the harness summary model
// actually emits: ``**bold**``, ``*italic*`` and `` `code` ``.
//
// Underscores are intentionally NOT treated as emphasis: these labels
// are full of snake_case identifiers (``idea_tree``, ``eval_cmd``,
// ``merge_experiment``) and ``_`` emphasis would mangle them. Scanning
// is closer-driven via ``indexOf`` — no backtracking regex — so it
// stays linear on adversarial input.

import { Fragment, type ReactNode } from "react";

type InlineToken =
  | { kind: "text"; value: string }
  | { kind: "strong"; value: string }
  | { kind: "em"; value: string }
  | { kind: "code"; value: string };

function tokenizeInline(text: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let buf = "";
  let i = 0;
  const flush = () => {
    if (buf) {
      tokens.push({ kind: "text", value: buf });
      buf = "";
    }
  };
  while (i < text.length) {
    const ch = text[i];
    // Code span: `…` — wins over emphasis so inner markers stay literal.
    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i) {
        flush();
        tokens.push({ kind: "code", value: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    // Bold: **…** — checked before single-* italic.
    if (ch === "*" && text[i + 1] === "*") {
      const end = text.indexOf("**", i + 2);
      if (end > i + 1) {
        flush();
        tokens.push({ kind: "strong", value: text.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
    }
    // Italic: *…* (asterisk only — underscores are left verbatim).
    if (ch === "*") {
      const end = text.indexOf("*", i + 1);
      if (end > i + 1) {
        flush();
        tokens.push({ kind: "em", value: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    // No token opened here — emit the char as literal text. An opener
    // without a closer (e.g. a lone ``*``) falls through to here and is
    // preserved verbatim.
    buf += ch;
    i += 1;
  }
  flush();
  return tokens;
}

/**
 * Render *text* with inline markdown emphasis as React nodes. Returns
 * the raw string untouched when it contains no inline markers, so
 * callers can drop it in place of ``{text}`` with no layout change.
 */
export function renderInlineMarkdown(
  text: string | null | undefined,
): ReactNode {
  if (!text || !/[*`]/.test(text)) return text ?? null;
  const tokens = tokenizeInline(text);
  return tokens.map((tok, idx) => {
    switch (tok.kind) {
      case "strong":
        return (
          <strong key={idx} className="font-semibold">
            {tok.value}
          </strong>
        );
      case "em":
        return <em key={idx}>{tok.value}</em>;
      case "code":
        return (
          <code
            key={idx}
            className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]"
          >
            {tok.value}
          </code>
        );
      default:
        return <Fragment key={idx}>{tok.value}</Fragment>;
    }
  });
}
