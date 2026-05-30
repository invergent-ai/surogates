// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//

/**
 * Per-turn ``<next_action>`` footer parser.
 *
 * The harness instructs the assistant to emit a single
 * ``<next_action complexity="low|medium|high">…</next_action>`` block at
 * the end of every assistant turn (see
 * ``surogates/harness/prompts/guidance/next_action.md``).  The block is
 * meant as harness metadata, not user-facing content, so the chat UI
 * strips it from the rendered markdown and surfaces it as a small
 * status pill instead.
 */

export type NextActionComplexity = "low" | "medium" | "high";
export type NextActionSummary = "show" | "hide";

export interface ParsedNextAction {
  /** Self-reported complexity of the assistant's planned next turn. */
  complexity: NextActionComplexity;
  /**
   * Whether the chat UI should render the auto-generated turn-recap
   * card below the message.  Defaults to ``"hide"`` so a missing or
   * malformed value never produces a redundant card -- the model has
   * to opt IN to the heavier UI affordance.
   */
  summary: NextActionSummary;
  /**
   * Body of the directive: a short sentence describing what the
   * assistant will do next, or the literal string ``"done"`` when
   * the turn is final.  Trimmed; never empty (falls back to ``"done"``
   * when the model emitted only whitespace).
   */
  body: string;
}

export interface NextActionStripResult {
  /** The message text with every ``<next_action>`` block removed. */
  cleaned: string;
  /**
   * The parsed last-block directive, or ``null`` when the message
   * contains no ``<next_action>`` block.  Older sessions and turns
   * where the model failed to emit the directive both yield ``null``;
   * the UI just renders the message body without a status pill.
   */
  action: ParsedNextAction | null;
  /**
   * Fallback narration line inferred from the first sentence of the
   * assistant content when no ``<next_action>`` block is present and
   * the first sentence looks intent-shaped ("I'll…", "Let me…", "I
   * need to…", etc.).  Lets the UI surface natural agent narration
   * even when the model ignores the structured directive.  ``null``
   * when nothing intent-shaped was found (UI shows no narration line).
   */
  inferredNarration: string | null;
}

// Capture the full opening tag's attribute list and body, then parse
// individual attributes via a second pass.  Two-step matching keeps
// the grammar tolerant of attribute order (``complexity`` first OR
// ``summary`` first) without an unwieldy single regex.
const _NEXT_ACTION_RE =
  /<next_action([^>]*)>([\s\S]*?)<\/next_action>\s*/gi;

const _ATTR_RE = /(\w+)="([^"]*)"/g;

const _VALID_COMPLEXITY: ReadonlySet<NextActionComplexity> = new Set([
  "low",
  "medium",
  "high",
]);

const _VALID_SUMMARY: ReadonlySet<NextActionSummary> = new Set([
  "show",
  "hide",
]);

function _parseAttrs(rawAttrs: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of rawAttrs.matchAll(_ATTR_RE)) {
    out[m[1].toLowerCase()] = m[2];
  }
  return out;
}

/**
 * Heuristic fallback: pull the first sentence of the assistant
 * content for use as a narration line when the model didn't emit a
 * structured ``<next_action>`` block.  Language-agnostic on purpose
 * -- agents that respond in Romanian, French, etc. would otherwise
 * never get a narration line if we gated on English verb leads.
 *
 * Caps at 200 chars so a long answer paragraph that happens to be
 * one sentence doesn't get surfaced as narration.  Returns ``null``
 * when there's no usable sentence within that bound.
 */
function _inferNarration(text: string): string | null {
  if (!text) return null;
  // Pull the first paragraph or first 240 chars, whichever is shorter,
  // then split on the first hard sentence terminator (./!/?).
  const head = text.trimStart().split(/\n\s*\n/, 1)[0].slice(0, 240);
  const endMatch = /([.!?])(\s|$)/.exec(head);
  const firstSentence = (endMatch
    ? head.slice(0, endMatch.index + 1)
    : head
  ).trim();
  if (!firstSentence || firstSentence.length > 200) return null;
  return firstSentence;
}

/**
 * Strip every ``<next_action>`` block from *text* and return both the
 * cleaned text and the parsed last block (if any).  Unknown complexity
 * values normalise to ``"medium"`` (the conservative middle ground);
 * unknown or missing summary values normalise to ``"hide"`` so the
 * recap card never renders without the model explicitly asking for it.
 *
 * When the model failed to emit a structured ``<next_action>`` block,
 * ``inferredNarration`` carries the first intent-shaped sentence from
 * the assistant content (when present) so the chat UI can render
 * natural narration regardless of model compliance.  The explicit
 * block always wins -- the heuristic only fires when ``action`` is
 * ``null``.
 *
 * Idempotent for streamed text: while the model is still emitting
 * tokens, an incomplete ``<next_action`` tag without the closing
 * ``</next_action>`` is left in place so the renderer can hide it on
 * the next chunk once the closer arrives.
 */
export function stripAndParseNextAction(text: string): NextActionStripResult {
  if (!text) {
    return { cleaned: text ?? "", action: null, inferredNarration: null };
  }

  let last: ParsedNextAction | null = null;
  // Walk every match so duplicates (model retried thinking pre-amble,
  // double-emitted the directive) all get stripped from the body.
  // The LAST one wins as the surfaced directive — matches the backend
  // parser convention.
  const matches = Array.from(text.matchAll(_NEXT_ACTION_RE));
  if (matches.length === 0) {
    return {
      cleaned: text,
      action: null,
      inferredNarration: _inferNarration(text),
    };
  }
  for (const match of matches) {
    const attrs = _parseAttrs(match[1] ?? "");
    const rawComplexity = (attrs["complexity"] ?? "").trim().toLowerCase();
    const complexity = (
      _VALID_COMPLEXITY.has(rawComplexity as NextActionComplexity)
        ? rawComplexity
        : "medium"
    ) as NextActionComplexity;
    const rawSummary = (attrs["summary"] ?? "").trim().toLowerCase();
    const summary = (
      _VALID_SUMMARY.has(rawSummary as NextActionSummary)
        ? rawSummary
        : "hide"
    ) as NextActionSummary;
    const body = (match[2] ?? "").trim() || "done";
    last = { complexity, summary, body };
  }

  const cleaned = text.replace(_NEXT_ACTION_RE, "").trimEnd();
  // Explicit block wins over the heuristic.
  return { cleaned, action: last, inferredNarration: null };
}
