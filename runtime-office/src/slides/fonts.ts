import type { EditorPage } from "../connector.js";

/** A font the document server has: its files, by face. */
export interface Faces { name: string; regular: string; italic: string | null; bold: string | null; boldItalic: string | null }
/** The document server's fonts by lower-case name, and the address its files are under. */
export interface FontList { base: string; faces: Map<string, Faces> }
/** A face a slide's text uses. */
export interface Face { family: string; bold: boolean; italic: boolean }

// The document server serves its font files with their first 32 bytes XORed with this key, and its editor undoes it
// as it loads them.
const KEY = [0xa0, 0x66, 0xd6, 0x20, 0x14, 0x96, 0x47, 0xfa, 0x95, 0x69, 0xb8, 0x50, 0xb0, 0x41, 0x49, 0x48];
const MAX_FILES = 64;
// Claude Slides heals an unknown family to the generic; a generic is a basic face every document server has.
const GENERICS: Record<string, string> = {
  "sans-serif": "Arial", serif: "Times New Roman", monospace: "Courier New", cursive: "Comic Sans MS", "system-ui": "Arial",
};

export function deobfuscate(data: Buffer): Buffer {
  const out = Buffer.from(data);
  for (let i = 0; i < 32 && i < out.length; i++) out[i] = out[i]! ^ KEY[i % 16]!;
  return out;
}

/** The family a CSS font stack lays text out in on the document server: its first family the server has (*has* maps
 * lower-case names to the server's own spelling), else its generic's basic face, else Arial. */
export function resolveFamily(stack: string, has: Map<string, string>): string {
  for (const part of stack.split(",")) {
    const name = part.trim().replace(/^["']|["']$/g, "").toLowerCase();
    const own = has.get(name) ?? GENERICS[name];
    if (own) return own;
  }
  return "Arial";
}

/** The fonts in AllFonts.js: `__fonts_files` names the files by index, and each `__fonts_infos` entry is a name with
 * the file index and face of its regular, italic, bold and bold-italic faces (-1 for none). */
export function parseAllFonts(source: string): Map<string, Faces> {
  const files = arrayAfter(source, "__fonts_files") as string[];
  const infos = arrayAfter(source, "__fonts_infos") as unknown[][];
  const file = (index: unknown): string | null => (typeof index === "number" && index >= 0 && files[index] !== undefined ? files[index]! : null);
  const faces = new Map<string, Faces>();
  for (const [name, regular, , italic, , bold, , boldItalic] of infos) {
    const own = file(regular);
    if (typeof name !== "string" || own === null) continue;
    faces.set(name.toLowerCase(), { name, regular: own, italic: file(italic), bold: file(bold), boldItalic: file(boldItalic) });
  }
  return faces;
}

// The array literal assigned to *name* in the script: from the "[" after its "=" to the matching "]".
function arrayAfter(source: string, name: string): unknown[] {
  const at = source.indexOf(name);
  const start = at < 0 ? -1 : source.indexOf("[", source.indexOf("=", at));
  if (start < 0) return [];
  let depth = 0;
  for (let i = start; i < source.length; i++) {
    if (source[i] === "[") depth++;
    else if (source[i] === "]" && --depth === 0) return JSON.parse(source.slice(start, i + 1)) as unknown[];
  }
  return [];
}

export class Fonts {
  private readonly lists = new Map<string, Promise<FontList>>();
  private readonly files = new Map<string, Promise<string>>();

  /** The fonts of the document server *editor* loaded from: its version's path, read from the editor's address. */
  list(editor: EditorPage): Promise<FontList> {
    const url = editor.editorUrl();
    const at = url.indexOf("/web-apps/");
    if (at < 0) return Promise.reject(new Error("the editor's address does not say where its fonts are"));
    const base = url.slice(0, at);
    let list = this.lists.get(base);
    if (!list) {
      list = editor.fetchBytes(`${base}/sdkjs/common/AllFonts.js`).then((bytes) => ({ base, faces: parseAllFonts(bytes.toString("utf8")) }));
      list.catch(() => this.lists.delete(base));
      this.lists.set(base, list);
    }
    return list;
  }

  /** @font-face rules for *wanted*, each from the document server's own file of that face. A face the server has no
   * file for is synthesized from the regular one, by the editor and by Chromium alike: only the regular rule is given. */
  async css(editor: EditorPage, list: FontList, wanted: Face[]): Promise<string> {
    const rules = new Map<string, string>();
    for (const face of wanted) {
      const faces = list.faces.get(face.family.toLowerCase());
      if (!faces) continue;
      const own = face.bold && face.italic ? faces.boldItalic : face.bold ? faces.bold : face.italic ? faces.italic : faces.regular;
      const [file, bold, italic] = own ? [own, face.bold, face.italic] : [faces.regular, false, false];
      const key = `${faces.name}|${bold}|${italic}`;
      if (rules.has(key)) continue;
      const data = await this.file(editor, `${list.base}/fonts/${file}`);
      rules.set(key, `@font-face{font-family:"${faces.name}";src:url(data:font/ttf;base64,${data});font-weight:${bold ? 700 : 400};font-style:${italic ? "italic" : "normal"}}`);
    }
    return [...rules.values()].join("\n");
  }

  private file(editor: EditorPage, url: string): Promise<string> {
    let file = this.files.get(url);
    if (!file) {
      if (this.files.size >= MAX_FILES) this.files.delete(this.files.keys().next().value!);
      file = editor.fetchBytes(url).then((bytes) => deobfuscate(bytes).toString("base64"));
      file.catch(() => this.files.delete(url));
      this.files.set(url, file);
    }
    return file;
  }
}
