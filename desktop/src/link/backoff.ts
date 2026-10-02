const BASE_MS = 1_000;
const CAP_MS = 60_000;

/** How long to wait before reconnect attempt *attempt* (0 first): doubling, capped, never below half. */
export function reconnectDelayMs(attempt: number, random: () => number = Math.random): number {
  const ceiling = Math.min(CAP_MS, BASE_MS * 2 ** attempt);
  return Math.round(ceiling * (0.5 + random() / 2));
}
