/**
 * Minimal, dependency-free Markdown → HTML renderer for assistant text.
 *
 * Assistant output is *semi-trusted* (it comes from the agent, not the
 * visitor), but the widget renders into a Shadow DOM via
 * ``dangerouslySetInnerHTML``, so a careless renderer would still be an
 * XSS vector if the model ever echoed attacker-supplied HTML.  The
 * contract here is therefore: **escape first, then re-introduce only the
 * small, known-safe subset of HTML the formatter produces.**  No raw
 * HTML from the source string ever survives — every ``<`` in the input
 * is entity-escaped before any tag is emitted.
 *
 * Supported: fenced code blocks, inline code, bold, italic, links
 * (http/https/mailto only), unordered + ordered lists, and paragraphs
 * with line breaks.  Everything else degrades to escaped plain text.
 */

const ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ESCAPE_MAP[c] ?? c);
}

/** Allow only links the browser can't be tricked into executing. */
function safeHref(raw: string): string | undefined {
  const url = raw.trim();
  if (/^(https?:|mailto:)/i.test(url)) return url;
  return undefined;
}

/**
 * Apply inline formatting to an *already HTML-escaped* fragment.  Order
 * matters: links before emphasis so ``[a](b)`` brackets aren't eaten by
 * the italic rule, and bold (``**``) before italic (``*``).
 */
function renderInline(escaped: string): string {
  let out = escaped;

  // Inline code first — its contents must not be touched by later rules.
  // The buffer is already escaped, so the captured group is literal text.
  out = out.replace(/`([^`]+)`/g, (_m, code: string) => `<code>${code}</code>`);

  // Links: [text](href).  ``href`` is validated; bad schemes render as
  // the original (escaped) text without an anchor.
  out = out.replace(
    /\[([^\]]+)\]\(([^)\s]+)\)/g,
    (_m, text: string, href: string) => {
      const safe = safeHref(href);
      if (!safe) return `${text}`;
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    },
  );

  out = out.replace(/\*\*([^*]+)\*\*/g, (_m, t: string) => `<strong>${t}</strong>`);
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, (_m, pre: string, t: string) => `${pre}<em>${t}</em>`);

  return out;
}

/**
 * Render ``src`` Markdown to a safe HTML string.  Splits on fenced code
 * blocks so their bodies bypass inline formatting entirely, then groups
 * the remaining text into list and paragraph blocks.
 */
export function renderMarkdown(src: string): string {
  const parts = src.split(/```/);
  const html: string[] = [];

  parts.forEach((part, index) => {
    const isCodeFence = index % 2 === 1;
    if (isCodeFence) {
      // Drop an optional language hint on the first line.
      const body = part.replace(/^[^\n]*\n/, (m) => (part.includes('\n') ? '' : m));
      html.push(`<pre><code>${escapeHtml(body.replace(/\n$/, ''))}</code></pre>`);
      return;
    }

    const blocks = part.split(/\n{2,}/);
    for (const block of blocks) {
      const lines = block.split('\n').filter((l) => l.trim().length > 0);
      if (lines.length === 0) continue;

      const isUnordered = lines.every((l) => /^\s*[-*]\s+/.test(l));
      const isOrdered = lines.every((l) => /^\s*\d+\.\s+/.test(l));
      if (isUnordered || isOrdered) {
        const items = lines
          .map((l) => l.replace(/^\s*(?:[-*]|\d+\.)\s+/, ''))
          .map((l) => `<li>${renderInline(escapeHtml(l))}</li>`)
          .join('');
        html.push(isOrdered ? `<ol>${items}</ol>` : `<ul>${items}</ul>`);
        continue;
      }

      const paragraph = lines.map((l) => renderInline(escapeHtml(l))).join('<br>');
      html.push(`<p>${paragraph}</p>`);
    }
  });

  return html.join('');
}
