import type { PagePool } from "../pages.js";
import { Measurer } from "../slides/measure.js";
import { cell } from "./cell.js";
import type { Format } from "./format.js";
import { pdf } from "./pdf.js";
import { slideFormat } from "./slide.js";
import { word } from "./word.js";

/** The formats by the editor's document type (the editor config's `documentType`). */
export function formatsFor(pool: PagePool): Record<string, Format> {
  return { word, cell, slide: slideFormat(new Measurer(() => pool.measuringPage())), pdf };
}
