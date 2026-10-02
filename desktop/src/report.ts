// Tell onError what failed. An onError that throws is swallowed: it was the
// last place to tell, so there is nothing further to do with its failure.

export function report(onError: ((error: unknown) => void) | undefined, error: unknown): void {
  try {
    onError?.(error);
  } catch {
    // Nothing is left to do with it.
  }
}
