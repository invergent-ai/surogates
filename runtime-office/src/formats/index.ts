import type { PagePool } from "../pages.js";
import { cell } from "./cell.js";
import type { Format } from "./format.js";
import { word } from "./word.js";

/** The formats by the editor's document type (the editor config's `documentType`). */
export function formatsFor(_pool: PagePool): Record<string, Format> {
  return { word, cell };
}
