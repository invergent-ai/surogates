import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { PagePool } from "../src/pages.js";
import { Fonts } from "../src/slides/fonts.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, type Fixture } from "./support/office.js";

describe.skipIf(!itEnabled)("the document server's fonts, through an editor page", () => {
  let fixture: Fixture;
  let pool: PagePool;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 60_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("lists them, and gives a face as its real font file", async () => {
    const key = newKey();
    const page = await pool.get(await pool.open({ docKey: key, sessionId: "s", config: editorConfig(key, AGENT, "blank.pptx"), documentServerUrl: IT.dsUrl }));
    const fonts = new Fonts();
    const list = await fonts.list(page);
    expect(list.faces.has("arial")).toBe(true);
    expect(list.faces.has("calibri")).toBe(true);
    const css = await fonts.css(page, list, [{ family: "Calibri", bold: false, italic: false }]);
    // A TrueType file starts 00 01 00 00, which is "AAEAAA" in base64.
    expect(css).toMatch(/^@font-face\{font-family:"Calibri";src:url\(data:font\/ttf;base64,AAEAAA/);
  });
});
