import { describe, expect, it } from "vitest";
import { deobfuscate, parseAllFonts, resolveFamily } from "../src/slides/fonts.js";

const SOURCE = `window["__fonts_files"] = ["000", "001", "002"];
window["__fonts_infos"] = [["Arial",0,0,1,0,2,0,-1,-1],["Ghost",-1,0,-1,-1,-1,-1,-1,-1]];`;

describe("the document server's fonts", () => {
  it("reads each name's files by face", () => {
    const faces = parseAllFonts(SOURCE);
    expect(faces.get("arial")).toEqual({ name: "Arial", regular: "000", italic: "001", bold: "002", boldItalic: null });
    expect(faces.has("ghost")).toBe(false);
  });

  it("undoes the obfuscation of a file's first 32 bytes, and only those", () => {
    const file = Buffer.alloc(40, 0);
    const served = deobfuscate(file);
    expect(served[0]).toBe(0xa0);
    expect(served[32]).toBe(0);
    expect(deobfuscate(served).equals(file)).toBe(true);
  });

  it("resolves a stack to its first family the server has, a generic to its basic face", () => {
    const has = new Map(["Arial", "Georgia", "Times New Roman", "Courier New", "Comic Sans MS", "Open Sans"].map((name) => [name.toLowerCase(), name]));
    expect(resolveFamily(`"DM Sans", "open sans", Arial, sans-serif`, has)).toBe("Open Sans");
    expect(resolveFamily(`"Source Serif 4", serif`, has)).toBe("Times New Roman");
    expect(resolveFamily("monospace", has)).toBe("Courier New");
    expect(resolveFamily(`"Nothing Here"`, has)).toBe("Arial");
  });
});
