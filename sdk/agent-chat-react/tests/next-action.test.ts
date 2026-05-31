import { describe, expect, it } from "vitest";

import { stripAndParseNextAction } from "../src/lib/next-action";

describe("stripAndParseNextAction", () => {
  it("strips a complete next_action footer", () => {
    const parsed = stripAndParseNextAction(
      'Answer.\n\n<next_action complexity="low" summary="hide">\ndone\n</next_action>',
    );

    expect(parsed.cleaned).toBe("Answer.");
    expect(parsed.action).toEqual({
      complexity: "low",
      summary: "hide",
      body: "done",
    });
  });

  it("hides an incomplete streaming next_action footer", () => {
    const parsed = stripAndParseNextAction(
      'Answer.\n\n<next_action complexity="low" summary="hide">',
    );

    expect(parsed.cleaned).toBe("Answer.");
    expect(parsed.action).toBeNull();
    expect(parsed.inferredNarration).toBeNull();
  });
});
