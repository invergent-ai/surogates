// Text as the app shows it, in its pages and in its native boxes alike: an age, and a string
// with each character that could hide or reorder what is around it marked as its code point.
// DOM-free, for the main process and the pages both.

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** How long ago *when* was: "17m" in a row, or "17 minutes ago" on a card. A time ahead of *now* is now. */
export function ago(when: string, now = Date.now(), form: "short" | "long" = "short"): string {
  const minutes = Math.floor((now - Date.parse(when)) / 60_000);
  if (minutes < 1) return form === "short" ? "now" : "just now";
  const [count, unit]: [number, Intl.RelativeTimeFormatUnit] = minutes < 60 ? [minutes, "minute"]
    : minutes < 24 * 60 ? [Math.floor(minutes / 60), "hour"] : [Math.floor(minutes / (24 * 60)), "day"];
  return form === "short" ? `${count}${unit[0]}` : RELATIVE.format(-count, unit);
}

// What a prompt shows as its code point rather than as itself: controls and format characters (the
// bidi controls among them), private-use, unassigned and lone surrogate code points, line and
// paragraph separators, what draws nothing (the blank symbols among it), and every space but U+0020.
const SPECIAL = /[\p{C}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}\u2800\uFFFC\u{1D159}]|(?! )\p{Zs}/gu;

// Three or more line breaks with nothing but spaces and tabs between them: blank lines, which
// would push what follows them out of view.
const BLANK_LINES = /\n(?:[ \t]*\n){2,}/g;

type Run = { text: string; special: boolean };

/**
 * *text* in runs: plain text, and each special character as its code point (U+202E). *keep*
 * holds the special characters shown as themselves. Where it keeps newlines, blank lines
 * show as one mark of how many line breaks they are (U+000A ×40), then one newline.
 */
export function segments(text: string, keep = ""): Run[] {
  const runs: Run[] = [];
  let last = 0;
  if (keep.includes("\n")) {
    for (const match of text.matchAll(BLANK_LINES)) {
      marked(text.slice(last, match.index), keep, runs);
      runs.push({ text: `U+000A ×${match[0].split("\n").length - 1}`, special: true }, { text: "\n", special: false });
      last = match.index + match[0].length;
    }
  }
  marked(text.slice(last), keep, runs);
  return runs;
}

// *text*'s runs onto *runs*, one push each: a text of any number of runs.
function marked(text: string, keep: string, runs: Run[]): void {
  let last = 0;
  for (const match of text.matchAll(SPECIAL)) {
    const [found] = match;
    if (keep.includes(found)) continue;
    if (match.index > last) runs.push({ text: text.slice(last, match.index), special: false });
    runs.push({ text: `U+${found.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}`, special: true });
    last = match.index + found.length;
  }
  if (last < text.length) runs.push({ text: text.slice(last), special: false });
}

/** *text* as showText shows it, in one string: what names it for a screen reader, or in a native box. */
export const asShown = (text: string, keep = ""): string => segments(text, keep).map((run) => run.text).join("");
