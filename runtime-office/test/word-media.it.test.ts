import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { compileOps } from "../src/ops.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;
const DOT = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

describe.skipIf(!itEnabled)("pictures and charts", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("adds a picture and a chart at the end", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const result: Any = await office.edit(agent, compileOps([{
      op: "insert",
      target: { end: true },
      content: {
        markdown: "<?block dot?>\n<?block growth?>",
        blocks: {
          dot: { type: "image", data: DOT, mime: "image/png", width: 40, height: 40 },
          growth: { type: "chart", chart: "bar", title: "Growth", rows: [["", "2025", "2026"], ["North", 10, 12], ["East", 8, 9]] },
        },
      },
    }]));
    expect(result.applied).toBe(true);
    const blocks: Any[] = ((await office.read(agent, { view: "outline" })) as Any).blocks;
    expect(blocks.slice(-2).map((b) => b.kind)).toEqual(["image", "image"]);
  });

  it("fits a tall picture within a page's height, keeping its shape", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const result: Any = await office.edit(agent, compileOps([{
      op: "insert",
      target: { end: true },
      content: { markdown: "<?block tall?>", blocks: { tall: { type: "image", data: DOT, mime: "image/png", width: 300, height: 1800 } } },
    }]));
    const size = await (await pool.get(agent)).command(
      "function () { var d = Api.GetByInternalId(scope.id).GetAllDrawingObjects()[0]; return [Math.round(d.GetWidth()), Math.round(d.GetHeight())]; }",
      { id: result.results[0].ids[0] },
    );
    // 576 px wide at most, and 864 px (nine inches) high: 300 x 1800 px becomes 144 x 864, in EMUs.
    expect(size.value).toEqual([144 * 9525, 864 * 9525]);
  });
});
