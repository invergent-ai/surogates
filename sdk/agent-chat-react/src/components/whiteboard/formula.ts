/**
 * LaTeX -> SVG -> raster, for `draw_formula` objects.
 *
 * A formula has to land in two places: on screen for the person, and
 * inside the atlas PNG the model reads back on the next turn. That rules
 * out a DOM overlay (invisible to the atlas) and rules out KaTeX, which
 * emits HTML/MathML and never SVG. MathJax is the same choice PenEcho
 * made, for the same reason.
 *
 * MathJax is loaded through a dynamic import so it lands in its own
 * chunk, the way Chart.js already does for artifacts: a consumer that
 * never draws a formula never downloads it. This is why `latexToSvg` is
 * async while `FormulaCache.get` stays synchronous — the render loop
 * must never await.
 *
 * `fontCache: "none"` inlines every glyph path rather than emitting
 * `<use>` references into a shared cache, so the SVG is self-contained
 * and cannot fail to rasterise inside a standalone `Image`.
 */

/** MathJax reports dimensions in `ex`; this is the assumed ex/em ratio. */
const EX_PER_EM = 0.45;

/** Bound the raster cache so a long session cannot grow without limit. */
const MAX_CACHED_FORMULAS = 200;

/**
 * Rough size for a formula whose glyphs have not been measured yet, so
 * bounds and hit-testing have something usable on the first frame. Self-
 * corrects to the exact size once the raster lands.
 */
const ESTIMATED_EM_PER_CHAR = 0.55;
const ESTIMATED_HEIGHT_EM = 1.6;

export interface FormulaSvg {
  svg: string;
  /** Intrinsic size in `em`, so the caller can scale by fontSize. */
  widthEm: number;
  heightEm: number;
}

type Converter = (latex: string, display: boolean) => string;

let converterPromise: Promise<Converter> | null = null;

/** Memoised dynamic import; the second caller reuses the first's promise. */
function loadConverter(): Promise<Converter> {
  if (converterPromise === null) {
    converterPromise = (async () => {
      const [
        { liteAdaptor },
        { RegisterHTMLHandler },
        { TeX },
        { mathjax },
        { SVG },
      ] = await Promise.all([
        import("mathjax-full/js/adaptors/liteAdaptor.js"),
        import("mathjax-full/js/handlers/html.js"),
        import("mathjax-full/js/input/tex.js"),
        import("mathjax-full/js/mathjax.js"),
        import("mathjax-full/js/output/svg.js"),
      ]);
      const adaptor = liteAdaptor();
      RegisterHTMLHandler(adaptor);
      const doc = mathjax.document("", {
        InputJax: new TeX({ packages: ["base", "ams"] }),
        OutputJax: new SVG({ fontCache: "none" }),
      });
      return (latex: string, display: boolean) =>
        adaptor.outerHTML(doc.convert(latex, { display }));
    })();
  }
  return converterPromise;
}

function exToEm(value: string | undefined): number {
  if (!value) return 1;
  const n = Number.parseFloat(value);
  if (!Number.isFinite(n)) return 1;
  return value.endsWith("ex") ? n * EX_PER_EM : n;
}

const EMPTY: FormulaSvg = { svg: "", widthEm: 0, heightEm: 0 };

/**
 * Convert LaTeX to a self-contained SVG string.
 *
 * Never rejects: malformed LaTeX comes back as MathJax's own error
 * markup, which is more useful on the canvas than a blank space.
 */
export async function latexToSvg(
  latex: string,
  display = true,
): Promise<FormulaSvg> {
  let outer: string;
  try {
    const convert = await loadConverter();
    outer = convert(latex, display);
  } catch {
    return EMPTY;
  }
  const start = outer.indexOf("<svg");
  const end = outer.lastIndexOf("</svg>");
  if (start === -1 || end === -1) return EMPTY;
  const svg = outer.slice(start, end + "</svg>".length);
  const dims = svg.match(/width="([^"]+)"\s+height="([^"]+)"/);
  return {
    svg,
    widthEm: exToEm(dims?.[1]),
    heightEm: exToEm(dims?.[2]),
  };
}

export interface RasterFormula {
  image: HTMLImageElement;
  /** Logical size at the requested font size. */
  w: number;
  h: number;
}

/**
 * Async raster cache for formulas.
 *
 * `get` is synchronous so the render loop never awaits: a miss returns
 * `null`, starts the conversion and decode, and calls `onReady` when the
 * glyphs are available so the caller can schedule one repaint. Drawing a
 * formula is therefore never on the critical path of a pen stroke.
 *
 * Keyed by the latex alone. The raster is an SVG image drawn at an
 * explicit size, so one entry serves every font size -- and it has to,
 * because dragging a resize handle walks the font size through a new
 * value on every pointer sample. Keyed by size as well, each of those
 * samples missed: the formula fell back to painting its own source
 * while the glyphs decoded, its bounds fell back to a character-count
 * estimate, and each frame queued another MathJax conversion that was
 * stale before it landed -- a flickering formula inside a selection
 * rectangle that jumped between the estimate and the truth.
 */
export class FormulaCache {
  private readonly entries = new Map<
    string,
    { image: HTMLImageElement; widthEm: number; heightEm: number }
  >();
  private readonly pending = new Set<string>();

  constructor(private readonly onReady: () => void) {}

  get(latex: string, fontSize: number): RasterFormula | null {
    const hit = this.entries.get(latex);
    if (hit) {
      return {
        image: hit.image,
        w: hit.widthEm * fontSize,
        h: hit.heightEm * fontSize,
      };
    }
    if (!this.pending.has(latex)) {
      this.pending.add(latex);
      void this.load(latex);
    }
    return null;
  }

  private async load(latex: string): Promise<void> {
    const key = latex;
    const { svg, widthEm, heightEm } = await latexToSvg(latex);
    if (!svg) {
      this.pending.delete(key);
      return;
    }

    const image = new Image();
    // A data: URL rather than a blob: URL — no object to revoke, and no
    // lifetime to get wrong when a cache entry is evicted mid-decode.
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    const settle = (ok: boolean) => {
      this.pending.delete(key);
      if (!ok) return;
      if (this.entries.size >= MAX_CACHED_FORMULAS) {
        const oldest = this.entries.keys().next().value;
        if (oldest !== undefined) this.entries.delete(oldest);
      }
      this.entries.set(key, { image, widthEm, heightEm });
      this.onReady();
    };
    image.onload = () => settle(true);
    image.onerror = () => settle(false);
  }

  /**
   * Size for layout and hit-testing, without waiting for the raster.
   *
   * Exact once the glyphs have landed; a cheap character-count estimate
   * before that, so selection chrome and hit-testing work on the first
   * frame and simply tighten on the next repaint.
   */
  measure(latex: string, fontSize: number): { w: number; h: number } {
    const hit = this.entries.get(latex);
    if (hit) {
      return { w: hit.widthEm * fontSize, h: hit.heightEm * fontSize };
    }
    return {
      w: Math.max(1, latex.length * ESTIMATED_EM_PER_CHAR * fontSize),
      h: ESTIMATED_HEIGHT_EM * fontSize,
    };
  }
}
