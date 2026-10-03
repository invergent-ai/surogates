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
