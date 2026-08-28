import { describe, expect, it } from "vitest";
import {
  resolveCommands,
  turnAnchorsFromMetadata,
} from "@/components/whiteboard/layout";
import { applyCommands, emptyDoc, foldToolCalls } from "@/components/whiteboard/doc";
import { makeCommandResolver } from "@/components/whiteboard/layout";
import type { AgentChatMessage } from "@/types";

const services = {
  formula: (latex: string, fontSize: number) => ({
    w: latex.length * fontSize * 0.5,
    h: fontSize * 1.2,
  }),
  formulaImage: () => null,
  createCanvas: (w: number, h: number) =>
    ({ width: w, height: h, getContext: () => null }) as unknown as
      HTMLCanvasElement,
};

type Cmd = Record<string, unknown>;
const one = (out: unknown[]): Cmd => out[0] as Cmd;

describe("resolving anchors", () => {
  // The `2x + 1 = 7` session: the user's line sat at (-190, -18) with
  // ~190-unit digits, and the model — converting image to canvas by
  // hand — landed its answer below the frame it was shown.
  const anchors = {
    latestInput: { x: -190, y: -18, w: 900, h: 190 },
    inkHeight: 193,
  };

  it("puts the answer right of the newest ink", () => {
    const [cmd] = resolveCommands(
      emptyDoc(),
      [{ tool: "draw_formula", latex: "3", anchor: "latest" }],
      anchors,
      services,
    ) as Cmd[];
    expect(cmd.x).toBeGreaterThan(-190 + 900);
    // Vertically within the line it answers, not below the board.
    expect(cmd.y as number).toBeGreaterThan(-18 - 190);
    expect(cmd.y as number).toBeLessThan(-18 + 190);
    expect(cmd.anchor).toBeUndefined();
  });

  it("sizes a short answer to the handwriting", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw_formula", latex: "3", anchor: "latest" }],
        anchors,
        services,
      ),
    );
    // The session's model chose 180 for 193-unit ink and the user
    // called the size right; the rule lands in the same band.
    const font = cmd.fontSize as number;
    expect(font).toBeGreaterThan(150);
    expect(font).toBeLessThanOrEqual(200);
  });

  it("sizes prose to read, never to match handwriting", () => {
    // fontSize 75 prose at maxWidth 400 was the nine-line tower. The
    // client now chooses both, and chooses a paragraph shape.
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{
          tool: "write_text", anchor: "latest", side: "below",
          text: "Yes! The integral of e^x is e^x + C because the derivative of e^x is e^x.",
        }],
        anchors,
        services,
      ),
    );
    const font = cmd.fontSize as number;
    const maxWidth = cmd.maxWidth as number;
    expect(font).toBeLessThanOrEqual(34);
    // Wide enough that the block reads across, not down.
    expect(maxWidth).toBeGreaterThan(font * 0.6 * 30);
    expect(cmd.y as number).toBeGreaterThan(-18 + 190);
  });

  it("a revision takes the replaced object's place", () => {
    const doc = applyCommands(
      emptyDoc(),
      [{ tool: "write_text", x: 1310, y: 338, text: "wrong",
         fontSize: 80, maxWidth: 300 }],
      1,
      "toolu_01X",
    );
    const cmd = one(
      resolveCommands(
        doc,
        [{ tool: "draw_formula", latex: "= e^2 + 1", replaces: "toolu_01X" }],
        null,
        services,
      ),
    );
    expect(cmd.x).toBe(1310);
    expect(cmd.y).toBe(338);
  });

  it("nudges off existing work instead of covering it", () => {
    const doc = applyCommands(
      emptyDoc(),
      [{ tool: "write_text", x: 760, y: -50, text: "already here",
         fontSize: 40, maxWidth: 400 }],
      1,
      "occupier",
    );
    const cmd = one(
      resolveCommands(
        doc,
        [{ tool: "draw_formula", latex: "3", anchor: "latest" }],
        anchors,
        services,
      ),
    );
    // Landed clear of the occupier's box rather than inside it.
    const est = services.formula("3", cmd.fontSize as number);
    const overlap =
      (cmd.x as number) < 760 + 400 &&
      (cmd.x as number) + est.w > 760 &&
      (cmd.y as number) < -50 + 54 &&
      (cmd.y as number) + est.h > -50;
    expect(overlap).toBe(false);
  });

  it("drops an anchored command whose anchor cannot be resolved", () => {
    // A guessed position is exactly the failure this module ends.
    const out = resolveCommands(
      emptyDoc(),
      [{ tool: "draw_formula", latex: "3", anchor: "latest" }],
      null,
      services,
    );
    expect(out[0]).toBeNull();
  });

  it("leaves absolute commands untouched", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "write_text", x: 5, y: 6, text: "hi",
           fontSize: 20, maxWidth: 100 }],
        anchors,
        services,
      ),
    );
    expect(cmd.x).toBe(5);
    expect(cmd.y).toBe(6);
  });

  it("explicit coordinates win over an anchor", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw_formula", latex: "3", x: 9, y: 8,
           fontSize: 40, anchor: "latest" }],
        anchors,
        services,
      ),
    );
    expect(cmd.x).toBe(9);
    expect(cmd.y).toBe(8);
  });
});

describe("anchors ride the turn's user message", () => {
  const metadata = {
    whiteboard: {
      latestInput: { x: 100, y: 50, w: 600, h: 120 },
      mode: "sketch",
    },
  };

  function userMessage(meta?: Record<string, unknown>): AgentChatMessage {
    return {
      id: "u1", role: "user", content: "", createdAt: new Date(),
      status: "complete", metadata: meta,
    } as AgentChatMessage;
  }

  function draw(id: string, commands: unknown[]): AgentChatMessage {
    return {
      id, role: "assistant", content: "", createdAt: new Date(),
      status: "complete",
      toolCalls: [{
        id, toolName: "whiteboard_draw",
        args: JSON.stringify({ commands }), status: "complete",
      }],
    } as unknown as AgentChatMessage;
  }

  it("resolves 'latest' from the preceding user message on fold", () => {
    // Replay-safe: the metadata is stored on the user.message event, so
    // a reload folds to exactly the same coordinates as the live turn.
    const doc = foldToolCalls(
      emptyDoc(),
      [
        userMessage(metadata),
        draw("c1", [{ tool: "draw_formula", latex: "3", anchor: "latest" }]),
      ],
      makeCommandResolver(services),
    );
    expect(doc.objects).toHaveLength(1);
    const obj = doc.objects[0] as { x: number; y: number };
    expect(obj.x).toBeGreaterThan(700);
  });

  it("consumes the call even when the anchor is missing", () => {
    // Dropped, not retried forever: the call is folded regardless.
    const doc = foldToolCalls(
      emptyDoc(),
      [
        userMessage(undefined),
        draw("c1", [{ tool: "draw_formula", latex: "3", anchor: "latest" }]),
      ],
      makeCommandResolver(services),
    );
    expect(doc.objects).toHaveLength(0);
    expect(doc.folded).toContain("c1");
  });

  it("parses only well-formed rects out of the metadata", () => {
    expect(
      turnAnchorsFromMetadata({ whiteboard: { latestInput: "wide" } }),
    ).toEqual({});
    expect(turnAnchorsFromMetadata(undefined)).toBeNull();
  });
});

describe("sizing and aligning to the handwriting, not the box", () => {
  // Session caae0d8d, turn 1: latestInput was 879x603 because the
  // expression had a tall integral sign, while the digits were 60. The
  // answer came out at fontSize 220.
  const tallBox = {
    latestInput: { x: -12, y: -55, w: 879, h: 603 },
    inkHeight: 60,
  };

  it("sizes the answer to the handwriting height, not the anchor box", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw_formula", latex: "e^x + x + C", anchor: "latest" }],
        tallBox,
        services,
      ),
    );
    expect(cmd.fontSize as number).toBeLessThanOrEqual(70);
    expect(cmd.fontSize as number).toBeGreaterThanOrEqual(50);
  });

  it("keeps the gap proportional to the handwriting too", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw_formula", latex: "3", anchor: "latest" }],
        tallBox,
        services,
      ),
    );
    // Right of the box, but not 200 units away from it.
    expect(cmd.x as number).toBeGreaterThan(-12 + 879);
    expect(cmd.x as number).toBeLessThan(-12 + 879 + 80);
  });

  it("aligns the answer to the line end, not the middle of the tallest stroke", () => {
    // `√(2⁴) =`: a radical spanning y 0..300 on the left and a short
    // `=` at y 130..150 on the right. The answer belongs on the `=`.
    const ink = (id: string, x: number, y: number, w: number, h: number) => ({
      id, origin: "local", selected: false, kind: "ink" as const,
      pts: [x, y, x + w, y + h], width: 0, color: "#000",
    });
    const doc = {
      ...emptyDoc(),
      objects: [ink("radical", 0, 0, 40, 300), ink("equals", 400, 130, 60, 20)],
    } as never;
    const anchors = {
      latestInput: { x: 0, y: 0, w: 460, h: 300 },
      inkHeight: 60,
    };
    const cmd = one(
      resolveCommands(
        doc,
        [{ tool: "draw_formula", latex: "4", anchor: "latest" }],
        anchors,
        services,
      ),
    );
    const est = services.formula("4", cmd.fontSize as number);
    const centre = (cmd.y as number) + est.h / 2;
    // Centred on the `=` (140), not on the box (150) or the radical.
    expect(Math.abs(centre - 140)).toBeLessThan(15);
    expect(cmd.x as number).toBeGreaterThan(460);
  });

  it("falls back to the anchor's own height when it is an agent object", () => {
    // No ink to measure: the earlier answer's line height stands in,
    // capped so a big answer does not breed a bigger one.
    const doc = applyCommands(
      emptyDoc(),
      [{ tool: "draw_formula", x: 0, y: 0, latex: "e^x", fontSize: 220 }],
      1,
      "big",
    );
    const cmd = one(
      resolveCommands(
        doc,
        [{ tool: "write_text", text: "ok", anchor: "big" }],
        null,
        services,
      ),
    );
    expect(cmd.fontSize as number).toBeLessThanOrEqual(120);
  });
});

describe("anchoring by label", () => {
  const anchors = {
    inkHeight: 60,
    marks: {
      A2: { rect: { x: 100, y: 400, w: 300, h: 60 } },
      B1: { rect: { x: 900, y: 40, w: 200, h: 50 }, origin: "toolu_01A" },
      B2: { rect: null, origin: "toolu_01B" },
    },
  };

  it("places right of a user-ink label", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw_formula", latex: "3", anchor: "A2" }],
        anchors,
        services,
      ),
    );
    expect(cmd.x as number).toBeGreaterThan(400);
    expect(cmd.y as number).toBeGreaterThan(300);
    expect(cmd.y as number).toBeLessThan(500);
  });

  it("resolves an agent label against the live board, not the snapshot", () => {
    // The user dragged B1 after the picture was taken; the answer must
    // follow the object, which is the point of anchoring by name.
    const doc = applyCommands(
      emptyDoc(),
      [{ tool: "draw_formula", x: 2000, y: 2000, latex: "e^x", fontSize: 50 }],
      1,
      "toolu_01A",
    );
    const cmd = one(
      resolveCommands(
        doc,
        [{ tool: "write_text", text: "yes", anchor: "B1" }],
        anchors,
        services,
      ),
    );
    expect(cmd.x as number).toBeGreaterThan(2000);
  });

  it("drops an anchor to a label the user has removed", () => {
    const out = resolveCommands(
      emptyDoc(),
      [{ tool: "write_text", text: "yes", anchor: "B2" }],
      anchors,
      services,
    );
    expect(out[0]).toBeNull();
  });

  it("translates a B-label replaces into the call id", () => {
    const doc = applyCommands(
      emptyDoc(),
      [{ tool: "draw_formula", x: 0, y: 0, latex: "wrong", fontSize: 50 }],
      1,
      "toolu_01A",
    );
    const cmd = one(
      resolveCommands(
        doc,
        [{ tool: "draw_formula", latex: "right", replaces: "B1" }],
        anchors,
        services,
      ),
    );
    expect(cmd.replaces).toBe("toolu_01A");
    expect(cmd.x).toBe(0);
  });

  it("parses marks out of the metadata", () => {
    const parsed = turnAnchorsFromMetadata({
      whiteboard: {
        marks: [
          { id: "A1", kind: "ink", x: 1, y: 2, w: 3, h: 4 },
          { id: "B1", kind: "agent", removed: true, origin: "c1" },
          { kind: "ink", x: 0, y: 0, w: 1, h: 1 },
        ],
      },
    });
    expect(parsed?.marks?.A1).toEqual({ rect: { x: 1, y: 2, w: 3, h: 4 } });
    expect(parsed?.marks?.B1).toEqual({ rect: null, origin: "c1" });
    expect(Object.keys(parsed?.marks ?? {})).toHaveLength(2);
  });
});

describe("text bounds account for wrapping", () => {
  // Session 87eb4165: the old answer wrapped to two lines but reported a
  // one-line box, so the nudge cleared its first line and landed the new
  // answer squarely on its second.
  const prose =
    "This integral appears incomplete. What should be under the square root?";

  it("keeps a new answer clear of a wrapped one", () => {
    const withOld = applyCommands(
      emptyDoc(),
      [{ tool: "write_text", x: 772, y: 269, text: prose,
         fontSize: 28.8, maxWidth: 726, lineHeight: 1.35 }],
      1,
      "old",
    );
    // The `=` at the end of the user's line: both answers target it,
    // which is what put the second on top of the first.
    const doc = {
      ...withOld,
      objects: [
        ...withOld.objects,
        { id: "eq", origin: "local", selected: false, kind: "ink" as const,
          pts: [700, 260, 740, 290], width: 0, color: "#000" },
      ],
    };
    const cmd = one(
      resolveCommands(
        doc,
        [{ tool: "write_text", anchor: "latest", side: "right",
           text: "This integral diverges (equals ∞). Both e^x and √x grow without bound." }],
        { latestInput: { x: 7, y: -140, w: 740, h: 647 }, inkHeight: 64 },
        services,
      ),
    );
    // Two lines of 28.8 at 1.35 is ~78 units: the new block must start
    // below the whole of the old one, not below its first line.
    expect(cmd.y as number).toBeGreaterThanOrEqual(269 + 77);
  });
});

describe("erasing one's own object", () => {
  const old = () =>
    applyCommands(
      emptyDoc(),
      [{ tool: "write_text", x: 772, y: 269, text: "an earlier answer",
         fontSize: 30, maxWidth: 400 }],
      1,
      "toolu_old",
    );

  it("turns an erase rect over it into a removal", () => {
    // Erase only paints white; the old answer would sit under the smear
    // and block the next placement. The model reaches for erase anyway.
    const out = resolveCommands(
      old(),
      [{ tool: "erase", mode: "rect", x: 772, y: 269, w: 726, h: 39 }],
      null,
      services,
    );
    expect(out).toHaveLength(1);
    expect((out[0] as Cmd).replaces).toBe("toolu_old");
    const doc = applyCommands(old(), out, 2, "toolu_new");
    expect(doc.objects.some((o) => o.origin === "toolu_old")).toBe(false);
  });

  it("leaves an erase that only clips a corner alone", () => {
    const out = resolveCommands(
      old(),
      [{ tool: "erase", mode: "rect", x: 1100, y: 290, w: 100, h: 100 }],
      null,
      services,
    );
    expect((out[0] as Cmd).replaces).toBeUndefined();
  });

  it("never removes the user's ink this way", () => {
    const doc = {
      ...emptyDoc(),
      objects: [{
        id: "s1", origin: "local", selected: false, kind: "ink" as const,
        pts: [0, 0, 100, 40], width: 0, color: "#000",
      }],
    };
    const out = resolveCommands(
      doc as never,
      [{ tool: "erase", mode: "rect", x: -10, y: -10, w: 200, h: 100 }],
      null,
      services,
    );
    expect((out[0] as Cmd).replaces).toBeUndefined();
  });
});

describe("filling a slot", () => {
  const slot = { x: 140, y: 20, w: 60, h: 70 };
  const anchors = {
    inkHeight: 60,
    marks: { S1: { rect: slot, objectId: "local:9" } },
  };

  it("centres a short answer inside the box, sized to it", () => {
    // `H [S1] USE`: the O goes in the gap, not beside the word.
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "write_text", text: "O", anchor: "S1", side: "in" }],
        anchors,
        services,
      ),
    );
    const est = { w: 1 * (cmd.fontSize as number) * 0.6, h: (cmd.fontSize as number) * 1.35 };
    expect(cmd.x as number).toBeGreaterThanOrEqual(slot.x);
    expect((cmd.x as number) + est.w).toBeLessThanOrEqual(slot.x + slot.w + 1);
    expect(cmd.y as number).toBeGreaterThanOrEqual(slot.y);
    expect((cmd.y as number) + est.h).toBeLessThanOrEqual(slot.y + slot.h + 1);
    expect(cmd.fillsSlot).toBe("local:9");
    expect(cmd.anchor).toBeUndefined();
  });

  it("shrinks text that would not fit the width", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "write_text", text: "HOUSEBOAT", anchor: "S1", side: "in" }],
        anchors,
        services,
      ),
    );
    const width = 9 * (cmd.fontSize as number) * 0.6;
    expect(width).toBeLessThanOrEqual(slot.w * 0.95 + 1);
  });

  it("gives an artifact the slot's exact box", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "place_artifact", artifact_id: "cat", w: 1, h: 1, anchor: "S1", side: "in" }],
        anchors,
        services,
      ),
    );
    expect([cmd.x, cmd.y, cmd.w, cmd.h]).toEqual([140, 20, 60, 70]);
  });

  it("scales a sketch from its 1000-unit local box into the slot", () => {
    // A circle at the centre of the local box lands at the centre of
    // the slot, at the slot's scale.
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw", anchor: "S1", side: "in",
           types: ["circle", "line"], items: [[500, 500, 400], [0, 0, 1000, 1000]] }],
        anchors,
        services,
      ),
    );
    expect(cmd.origin).toEqual([140, 20]);
    const [circle, line] = cmd.items as number[][];
    expect(circle).toEqual([30, 35, 24]);
    expect(line).toEqual([0, 0, 60, 70]);
  });

  it("removes the slot when its fill is applied", () => {
    const doc = {
      ...emptyDoc(),
      objects: [{ id: "local:9", origin: "local", selected: false, kind: "slot" as const,
                  x: 140, y: 20, w: 60, h: 70 }],
    };
    const resolved = resolveCommands(
      doc,
      [{ tool: "write_text", text: "O", anchor: "S1", side: "in" }],
      anchors,
      services,
    );
    const next = applyCommands(doc, resolved, 2, "fillcall");
    expect(next.objects.some((o) => o.kind === "slot")).toBe(false);
    expect(next.objects.some((o) => o.kind === "text")).toBe(true);
  });

  it("parses the slot's object id out of the metadata", () => {
    const parsed = turnAnchorsFromMetadata({
      whiteboard: { marks: [{ id: "S1", kind: "slot", x: 1, y: 2, w: 3, h: 4, objectId: "local:9" }] },
    });
    expect(parsed?.marks?.S1).toEqual({ rect: { x: 1, y: 2, w: 3, h: 4 }, objectId: "local:9" });
  });
});


describe("formula height", () => {
  // A stacked fraction at handwriting font size renders ~2.5 lines tall
  // and dwarfs the expression it answers.
  const stacked = {
    ...services,
    formula: (latex: string, fontSize: number) => ({
      w: latex.length * fontSize * 0.5,
      h: latex.includes("\\frac") ? fontSize * 2.5 : fontSize * 1.2,
    }),
  };

  it("caps a tall fraction to the height of the writing", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw_formula", latex: "\\frac{x^2}{2} + C", anchor: "latest" }],
        { latestInput: { x: 0, y: 0, w: 900, h: 300 }, inkHeight: 131 },
        stacked,
      ),
    );
    const rendered = stacked.formula("\\frac{x^2}{2} + C", cmd.fontSize as number).h;
    expect(rendered).toBeLessThanOrEqual(131 * 1.6 + 1);
  });

  it("leaves a one-line formula at handwriting size", () => {
    const cmd = one(
      resolveCommands(
        emptyDoc(),
        [{ tool: "draw_formula", latex: "3", anchor: "latest" }],
        { latestInput: { x: 0, y: 0, w: 900, h: 300 }, inkHeight: 131 },
        stacked,
      ),
    );
    expect(cmd.fontSize).toBe(131);
  });
});
