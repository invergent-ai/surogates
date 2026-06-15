/**
 * Strip the per-turn ``<next_action>`` footer from assistant text.
 *
 * The harness instructs the assistant to end every final response with a
 * ``<next_action complexity="..." summary="...">...</next_action>`` block
 * (see ``surogates/harness/prompts/guidance/next_action.md``).  It is
 * harness/UI metadata, not user-facing content — by design the *client*
 * strips it: the full chat console does so in
 * ``agent-chat-react/src/lib/next-action.ts``, and messaging channels
 * strip it server-side.  This widget streams raw ``llm.delta`` frames, so
 * it must strip it too or the XML footer flashes in the bubble.
 *
 * We only need the stripping half here (the console additionally parses
 * the block into a status pill).  Streaming-safe: a dangling, not-yet-
 * closed footer is hidden the moment its opener appears so partial tags
 * never flicker.
 */

// Complete ``<next_action ...>...</next_action>`` blocks (+ trailing ws).
const NEXT_ACTION_RE = /<next_action([^>]*)>([\s\S]*?)<\/next_action>\s*/gi;

// A trailing, still-streaming footer: any prefix of the literal
// ``<next_action`` token at the start of a line through end-of-text,
// then everything after the whole word arrives (attributes + partial
// body, closing tag not yet seen).  Anchored to start-of-line so a stray
// ``<`` mid-sentence is never mistaken for the footer.
const PARTIAL_FOOTER_RE =
  /(?:^|\n)\s*<(?:n(?:e(?:x(?:t(?:_(?:a(?:c(?:t(?:i(?:o(?:n[\s\S]*)?)?)?)?)?)?)?)?)?)?)?$/i;

/** Return *text* with every (complete or streaming-partial) footer removed. */
export function stripNextAction(text: string): string {
  if (!text) return text;
  const withoutBlocks = text.replace(NEXT_ACTION_RE, '').trimEnd();
  const match = PARTIAL_FOOTER_RE.exec(withoutBlocks);
  return match ? withoutBlocks.slice(0, match.index).trimEnd() : withoutBlocks;
}
