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
