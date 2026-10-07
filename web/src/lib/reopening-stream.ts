// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// An event stream that opens itself again when it fails: the inbox's, and a project's. The
// SDK's inbox hook takes ``onerror`` as the stream disconnected for good, and its project hook
// as the project gone, so reconnecting is this wrapper's. It opens the stream again after every
// failure, a watchdog-detected silent stall included, and surfaces ``onerror`` only when its
// policy says to stop. It is given the way to open one connection, so the desktop's
// ProjectsSource follows a project the same way, and a test can drive it.

export interface StreamEvent {
  data: string;
  lastEventId?: string;
}

export interface EventStreamLike<T extends string> {
  addEventListener(type: T, listener: (event: StreamEvent) => void): void;
  close(): void;
  onerror: (() => void) | null;
}

export interface Reopening {
  /** How long to wait before opening again, after *failures* in a row with no event between them. */
  delayMs(failures: number): number;
  /** Whether to stop instead, and surface ``onerror``. */
  stop(failures: number): boolean;
}

/** The inbox's: every three seconds, three times in a row, before its hook says it is disconnected. */
export const INBOX_REOPENING: Reopening = { delayMs: () => 3_000, stop: (failures) => failures > 3 };

/**
 * A project's: three seconds after the first failure and twice as long after each next one, at
 * most a minute apart. It never gives up until *gone* says the project is gone.
 */
export function projectReopening(gone: () => boolean): Reopening {
  return { delayMs: (failures) => Math.min(3_000 * 2 ** (failures - 1), 60_000), stop: () => gone() };
}

type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/**
 * Whether a project's route answered that the project is gone: its own 404. An ingress with no
 * ready pod answers 404 too, in plain text, and an API pod without the route a 404 of its own;
 * those are an outage, retried like any other.
 */
export async function projectGone(response: Response): Promise<boolean> {
  if (response.status !== 404) return false;
  try {
    return ((await response.clone().json()) as { detail?: unknown }).detail === "No such project.";
  } catch {
    return false;
  }
}

/**
 * A project's stream, each connection opened by *open* over *fetchFn*. It opens itself again
 * after any failure, and ends, with ``onerror``, only once its route says the project is gone.
 */
export function projectStream<T extends string>(
  open: (fetchFn: Fetch) => EventStreamLike<T>,
  fetchFn: Fetch,
): EventStreamLike<T> {
  let gone = false;
  const watched: Fetch = async (input, init) => {
    const response = await fetchFn(input, init);
    gone = await projectGone(response);
    return response;
  };
  return reopeningStream(() => open(watched), projectReopening(() => gone));
}

export function reopeningStream<T extends string>(
  open: () => EventStreamLike<T>,
  reopening: Reopening,
): EventStreamLike<T> {
  const handlers = new Map<T, Set<(event: StreamEvent) => void>>();
  let source: EventStreamLike<T> | null = null;
  let closed = false;
  let failures = 0;
  let surfaced: (() => void) | null = null;

  // Any event proves the stream healthy again.
  const heard = (listener: (event: StreamEvent) => void) => (event: StreamEvent) => {
    failures = 0;
    listener(event);
  };

  function connect(): void {
    if (closed) return;
    const next = open();
    source = next;
    for (const [type, listeners] of handlers) {
      for (const listener of listeners) next.addEventListener(type, heard(listener));
    }
    next.onerror = () => {
      if (closed) return;
      next.close();
      if (source === next) source = null;
      failures += 1;
      if (reopening.stop(failures)) {
        surfaced?.();
        return;
      }
      setTimeout(connect, reopening.delayMs(failures));
    };
  }

  connect();

  return {
    addEventListener(type, listener) {
      const listeners = handlers.get(type) ?? new Set();
      listeners.add(listener);
      handlers.set(type, listeners);
      source?.addEventListener(type, heard(listener));
    },
    close() {
      closed = true;
      source?.close();
      source = null;
    },
    get onerror() {
      return surfaced;
    },
    set onerror(handler: (() => void) | null) {
      surfaced = handler;
    },
  };
}
