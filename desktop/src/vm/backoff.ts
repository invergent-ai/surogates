// How long a start waits after what it starts went (spec, Section 11, Lifecycle): 1 s
// after the first that went, doubling with each that goes again to at most 60 s, and from
// 1 s again once one stayed up 30 s. The VM manager's process (client.ts) and each boot of
// the guest (manager.ts) keep one each, so a start that keeps failing does not loop.

const FIRST_MS = 1_000;
export const MOST_MS = 60_000;
const STAYED_MS = 30_000;

export class Backoff {
  private failures = 0;
  private notBefore = 0;
  private upSince: number | null = null;

  constructor(private readonly now: () => number = () => performance.now()) {}

  /** How long the next start waits, in milliseconds: 0 once it may start. */
  get wait(): number {
    return Math.max(0, this.notBefore - this.now());
  }

  /** What was started is up. */
  up(): void {
    this.upSince = this.now();
  }

  /** What was started went by itself, or never came up: the next start waits. */
  down(): void {
    const stayed = this.upSince !== null && this.now() - this.upSince >= STAYED_MS;
    this.upSince = null;
    this.failures = stayed ? 1 : this.failures + 1;
    this.notBefore = this.now() + Math.min(MOST_MS, FIRST_MS * 2 ** (this.failures - 1));
  }
}
