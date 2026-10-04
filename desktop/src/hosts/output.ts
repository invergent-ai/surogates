// A command's output as the reference laptop shapes it (tests/fake_laptop.py
// _cap_text): stdout, then a newline and stderr when both have something, whole
// when it fits OUTPUT_CAP_CHARS measured JSON-encoded, else a head and a tail
// around a "chars omitted by the computer" marker. Python counts and slices code
// points, and so does this. Each stream is read through a window that keeps only
// what the cap can use, so a chatty command is never held in memory.

import { OUTPUT_CAP_CHARS, pyJsonLength } from "../files/answers.js";

// The most code points the cap keeps at either end.
const K = OUTPUT_CAP_CHARS / 2;

const marker = (omitted: number) => `\n... [${omitted} chars omitted by the computer] ...\n`;

// _cap_text, for a text held whole.
export function capText(text: string): string {
  if (pyJsonLength(text) <= OUTPUT_CAP_CHARS) return text;
  const points = Array.from(text);
  return capParts(points, points, points.length);
}

// _cap_text's search for the longest head and tail that fit. It only ever looks
// at the first and last K code points, which is what makes a window exact.
function capParts(head: string[], tail: string[], total: number): string {
  let half = K;
  while (half) {
    const size = pyJsonLength(head.slice(0, half).join("")) + pyJsonLength(tail.slice(tail.length - half).join(""));
    if (size <= OUTPUT_CAP_CHARS) break;
    half = Math.min(half - 1, Math.floor((half * OUTPUT_CAP_CHARS) / size));
  }
  return head.slice(0, half).join("") + marker(total - 2 * half) + tail.slice(tail.length - half).join("");
}

// One stream, decoded as Python's errors="replace" decodes it.
export class Window {
  total = 0;
  private readonly decoder = new TextDecoder("utf-8", { ignoreBOM: true });
  private head: string[] = [];
  private tail: string[] = [];

  push(chunk: Buffer): void {
    this.add(this.decoder.decode(chunk, { stream: true }));
  }

  end(): void {
    this.add(this.decoder.decode());
  }

  // All of it is held while there are no more than 2K code points.
  get whole(): boolean {
    return this.total <= 2 * K;
  }

  all(): string[] {
    return [...this.head, ...this.tail];
  }

  first(count: number): string[] {
    return this.head.slice(0, count);
  }

  last(count: number): string[] {
    if (count <= 0) return [];
    return (this.whole ? this.all() : this.tail).slice(-count);
  }

  private add(text: string): void {
    for (const point of text) {
      this.total += 1;
      if (this.head.length < K) this.head.push(point);
      else this.tail.push(point);
    }
    if (this.tail.length > 2 * K) this.tail = this.tail.slice(-K);
  }
}

// _cap_text of stdout, then "\n" and stderr when both have something.
export function commandOutput(out: Window, err: Window): string {
  const sep = out.total > 0 && err.total > 0 ? 1 : 0;
  const glue = sep ? ["\n"] : [];
  const total = out.total + sep + err.total;
  if (total <= 2 * K) return capText([...out.all(), ...glue, ...err.all()].join(""));
  const head = out.total >= K ? out.first(K) : [...out.all(), ...glue, ...err.first(K - out.total - sep)];
  const tail = err.total >= K ? err.last(K) : [...out.last(K - err.total - sep), ...glue, ...err.all()];
  return capParts(head, tail, total);
}

// _cap_strings: every string in a process outcome, at any depth, capped, and
// made well-formed (a lone surrogate cannot cross the link).
export function capStrings(value: unknown): unknown {
  if (typeof value === "string") return capText(value.toWellFormed());
  if (Array.isArray(value)) return value.map(capStrings);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, capStrings(item)]));
  }
  return value;
}

// Python's text[-count:], in code points.
export function lastPoints(text: string, count: number): string {
  let at = text.length;
  for (let seen = 0; seen < count && at > 0; seen += 1) {
    const unit = text.charCodeAt(at - 1);
    const pair = at > 1 && unit >= 0xdc00 && unit <= 0xdfff && (text.charCodeAt(at - 2) & 0xfc00) === 0xd800;
    at -= pair ? 2 : 1;
  }
  return text.slice(at);
}

// Python's text[:count], in code points.
export function firstPoints(text: string, count: number): string {
  return Array.from(text.slice(0, 2 * count)).slice(0, count).join("");
}

// Python's str.splitlines().
export function splitLines(text: string): string[] {
  const lines = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

const CSI_PARAM = (unit: number) => unit >= 0x30 && unit <= 0x3f;
const CSI_INTERMEDIATE = (unit: number) => unit >= 0x20 && unit <= 0x2f;
const CSI_FINAL = (unit: number) => unit >= 0x40 && unit <= 0x7e;

// The cloud's strip_ansi (surogates/tools/utils/ansi_strip.py), as one pass over
// the text: its regex, run by a backtracking engine, takes quadratic time on many
// escapes that never end, and a process's output is the agent's to shape.
export function stripAnsi(text: string): string {
  if (!/[\x1b\x80-\x9f]/.test(text)) return text;
  // Where the next terminator is, or -1 once none is left: each is looked for from
  // later and later places, so each search goes over the text once in all.
  const next = new Map<string, number>();
  const find = (terminators: string[], from: number): number => {
    let best = -1;
    let bestLength = 0;
    for (const terminator of terminators) {
      let at = next.get(terminator);
      if (at === undefined || (at !== -1 && at < from)) {
        at = text.indexOf(terminator, from);
        next.set(terminator, at);
      }
      if (at !== -1 && (best === -1 || at < best)) {
        best = at;
        bestLength = terminator.length;
      }
    }
    return best === -1 ? -1 : best + bestLength;
  };
  // A CSI's parameters, intermediates and final byte from *at*: where it ends, or -1.
  const csi = (at: number): number => {
    let i = at;
    while (i < text.length && CSI_PARAM(text.charCodeAt(i))) i += 1;
    while (i < text.length && CSI_INTERMEDIATE(text.charCodeAt(i))) i += 1;
    return i < text.length && CSI_FINAL(text.charCodeAt(i)) ? i + 1 : -1;
  };
  let out = "";
  let i = 0;
  while (i < text.length) {
    const unit = text.charCodeAt(i);
    let end = -1;
    if (unit === 0x1b && i + 1 < text.length) {
      const kind = text.charCodeAt(i + 1);
      if (kind === 0x5b) end = csi(i + 2);
      else if (kind === 0x5d) end = find(["\x07", "\x1b\\"], i + 2);
      else if (kind === 0x50 || kind === 0x58 || kind === 0x5e || kind === 0x5f) end = find(["\x1b\\"], i + 2);
      else if (CSI_INTERMEDIATE(kind)) {
        let j = i + 1;
        while (j < text.length && CSI_INTERMEDIATE(text.charCodeAt(j))) j += 1;
        if (j < text.length && text.charCodeAt(j) >= 0x30 && text.charCodeAt(j) <= 0x7e) end = j + 1;
      }
      // Fp, Fe and Fs: also what an opener that never ends falls back to.
      if (end === -1 && kind >= 0x30 && kind <= 0x7e) end = i + 2;
    } else if (unit === 0x9b) {
      end = csi(i + 1);
    } else if (unit === 0x9d) {
      end = find(["\x07", "\x9c"], i + 1);
    }
    if (end === -1 && unit >= 0x80 && unit <= 0x9f) end = i + 1;
    if (end === -1) {
      out += text[i];
      i += 1;
    } else {
      i = end;
    }
  }
  return out;
}
