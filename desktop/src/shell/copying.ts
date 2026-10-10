// The sidebar's line while threads' copies of their folders are being made (history/copies.ts, Making): a
// thread's first step waits for its copy, and a first copy of a large folder takes minutes. A making is said
// once it has run a moment, so a copy the guest only looks at, which is soon done, says nothing. Electron-free.

import type { Making } from "../history/copies.js";

// How long a making runs before the sidebar says it.
export const SAID_AFTER_MS = 1_000;

interface Under {
  folder: string;
  said: boolean;
  timer: NodeJS.Timeout;
}

export class Copying {
  // Each thread's making under way, by its root.
  private readonly making = new Map<string, Under>();

  /** *changed*: told each time the line may say something else. */
  constructor(private readonly changed: () => void, private readonly afterMs = SAID_AFTER_MS) {}

  heard(event: Making): void {
    const under = this.making.get(event.root);
    if (event.state === "begun") {
      // Begun again before it was said to end: the same making, still under way.
      if (under) return;
      const begun: Under = {
        folder: event.folder,
        said: false,
        timer: setTimeout(() => {
          begun.said = true;
          this.changed();
        }, this.afterMs),
      };
      begun.timer.unref();
      this.making.set(event.root, begun);
      return;
    }
    if (!under) return;
    clearTimeout(under.timer);
    this.making.delete(event.root);
    if (under.said) this.changed();
  }

  /** What the sidebar says, or null while no making has run a moment. */
  line(): string | null {
    const said = [...this.making.values()].filter((under) => under.said);
    const folders = [...new Set(said.map((under) => under.folder))];
    if (folders.length === 0) return null;
    if (folders.length > 1) return `Making copies of ${folders.length} folders for threads to work in`;
    return said.length === 1 ? `Making a copy of ${folders[0]} for a thread to work in` : `Making copies of ${folders[0]} for ${said.length} threads to work in`;
  }
}
