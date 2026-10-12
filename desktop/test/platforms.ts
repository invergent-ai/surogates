// A module as another OS imports it: each per-OS seam picks its side once, as it is imported.

import { vi } from "vitest";

/** What *load* imports, afresh, while process.platform is *platform*. */
export async function importedOn<T>(platform: NodeJS.Platform, load: () => Promise<T>): Promise<T> {
  const own = Object.getOwnPropertyDescriptor(process, "platform")!;
  vi.resetModules();
  Object.defineProperty(process, "platform", { ...own, value: platform });
  try {
    return await load();
  } finally {
    Object.defineProperty(process, "platform", own);
    vi.resetModules();
  }
}
