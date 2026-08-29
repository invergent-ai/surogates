import { beforeAll, describe, expect, it, vi } from "vitest";
import { FormulaCache, latexToSvg } from "@/components/whiteboard/formula";

describe("latexToSvg", () => {
  it("produces an svg element", async () => {
    const { svg } = await latexToSvg("x^2");
    expect(svg.startsWith("<svg")).toBe(true);
    expect(svg.endsWith("</svg>")).toBe(true);
  });

  it("reports an intrinsic size in em", async () => {
    const { widthEm, heightEm } = await latexToSvg("\\frac{x^2}{2}");
    expect(widthEm).toBeGreaterThan(0);
    expect(heightEm).toBeGreaterThan(0);
  });

  it("gives a taller box to a fraction than to a plain symbol", async () => {
    expect((await latexToSvg("\\frac{x}{y}")).heightEm)
      .toBeGreaterThan((await latexToSvg("x")).heightEm);
  });

  it("emits a self-contained svg with no unresolved glyph references", async () => {
    // fontCache "none" inlines every path. A dangling <use href="#..">
    // renders blank inside a standalone Image, which is exactly the
    // failure this setting exists to prevent.
    const { svg } = await latexToSvg("\\sum_{i=1}^{n} i^2");
    const refs = [...svg.matchAll(/xlink:href="#([^"]+)"/g)].map((m) => m[1]);
    const defs = [...svg.matchAll(/<path id="([^"]+)"/g)].map((m) => m[1]);
    expect(refs.filter((r) => !defs.includes(r))).toEqual([]);
  });

  it("does not reject on malformed latex", async () => {
    await expect(latexToSvg("\\frac{")).resolves.toBeDefined();
  });

  it("returns renderable markup for malformed latex", async () => {
    // MathJax's own error markup on the canvas beats a blank space.
    const { svg } = await latexToSvg("\\nosuchmacro{x}");
    expect(svg.startsWith("<svg")).toBe(true);
  });

  it("handles an empty string without rejecting", async () => {
    await expect(latexToSvg("")).resolves.toBeDefined();
  });
});

describe("FormulaCache", () => {
  /** Let the memoised MathJax import and the conversion settle. */
  async function settle() {
    await vi.waitFor(() => expect(converterReady).toBe(true));
    for (let i = 0; i < 5; i++) await Promise.resolve();
  }

  let converterReady = false;
  beforeAll(async () => {
    // Prime the memoised dynamic import once so per-test waits are short.
    await latexToSvg("x");
    converterReady = true;
  });

  /** Replace Image with a stub, run body, always restore. */
  async function withImage<T extends object>(
    Stub: new () => T,
    body: () => Promise<void>,
  ) {
    const Original = globalThis.Image;
    globalThis.Image = Stub as unknown as typeof Image;
    try {
      await body();
    } finally {
      globalThis.Image = Original;
    }
  }

  it("misses on first ask and does not block", () => {
    const cache = new FormulaCache(() => undefined);
    expect(cache.get("x^2", 32)).toBeNull();
  });

  it("measures without waiting for the raster", () => {
    const cache = new FormulaCache(() => undefined);
    const size = cache.measure("x^2", 40);
    expect(size.w).toBeGreaterThan(0);
    expect(size.h).toBeGreaterThan(0);
  });

  it("scales the measured size with font size", () => {
    const cache = new FormulaCache(() => undefined);
    const small = cache.measure("x^2", 20);
    const large = cache.measure("x^2", 40);
    expect(large.h).toBeGreaterThan(small.h);
  });

  it("starts only one decode per key", async () => {
    // Two asks in the same frame must not queue two image loads.
    const cache = new FormulaCache(() => undefined);
    const created: unknown[] = [];
    class CountingImage {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(v: string) {
        created.push(v);
      }
    }
    await withImage(CountingImage, async () => {
      cache.get("x^2", 32);
      cache.get("x^2", 32);
      await settle();
      expect(created).toHaveLength(1);
    });
  });

  it("rasterises once and scales the one raster to any font size", async () => {
    // Keyed by size too, a resize drag missed on every pointer sample:
    // the formula flickered back to its own source while each new size
    // decoded, and its bounds flickered with it.
    const cache = new FormulaCache(() => undefined);
    const created: unknown[] = [];
    class CountingImage {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(v: string) {
        created.push(v);
      }
    }
    await withImage(CountingImage, async () => {
      cache.get("x^2", 32);
      cache.get("x^2", 48);
      await settle();
      expect(created).toHaveLength(1);
    });
  });

  it("measures and draws the same glyphs at any size, in proportion", async () => {
    const cache = new FormulaCache(() => undefined);
    await withImage(class {
      onerror: (() => void) | null = null;
      set onload(fn: () => void) {
        fn();
      }
      set src(_v: string) {
        /* no-op */
      }
    }, async () => {
      cache.get("x^2", 32);
      await settle();
      const small = cache.get("x^2", 32);
      const big = cache.get("x^2", 64);
      expect(small).not.toBeNull();
      expect(big).not.toBeNull();
      // One raster, two sizes: no miss, so no fallback to the source.
      expect(big?.image).toBe(small?.image);
      expect(big?.w).toBeCloseTo((small?.w ?? 0) * 2, 5);
      expect(cache.measure("x^2", 64).h).toBeCloseTo((big?.h ?? 0), 5);
    });
  });

  it("signals readiness so the caller can repaint once", async () => {
    const onReady = vi.fn();
    const cache = new FormulaCache(onReady);
    let handler: (() => void) | null = null;
    class ManualImage {
      onerror: (() => void) | null = null;
      set onload(fn: () => void) {
        handler = fn;
      }
      set src(_v: string) {
        /* no-op */
      }
    }
    await withImage(ManualImage, async () => {
      cache.get("x^2", 32);
      await settle();
      expect(onReady).not.toHaveBeenCalled();
      handler?.();
      expect(onReady).toHaveBeenCalledTimes(1);
      expect(cache.get("x^2", 32)).not.toBeNull();
    });
  });

  it("does not cache a failed decode", async () => {
    const onReady = vi.fn();
    const cache = new FormulaCache(onReady);
    let fail: (() => void) | null = null;
    class FailingImage {
      onload: (() => void) | null = null;
      set onerror(fn: () => void) {
        fail = fn;
      }
      set src(_v: string) {
        /* no-op */
      }
    }
    await withImage(FailingImage, async () => {
      cache.get("x^2", 32);
      await settle();
      fail?.();
      expect(onReady).not.toHaveBeenCalled();
      expect(cache.get("x^2", 32)).toBeNull();
    });
  });
});
