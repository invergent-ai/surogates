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

  it("hides a partially-revealed opening tag during char-by-char streaming", () => {
    // useSmoothStream reveals the footer one character at a time, so the
    // body passes through every prefix of "<next_action" before the full
    // word arrives.  None of these prefixes must leak into the rendered
    // markdown.
    const prefixes = [
      "<",
      "<n",
      "<ne",
      "<nex",
      "<next",
      "<next_",
      "<next_a",
      "<next_ac",
      "<next_act",
      "<next_acti",
      "<next_actio",
      "<next_action",
    ];
    for (const p of prefixes) {
      const parsed = stripAndParseNextAction(`Answer.\n\n${p}`);
      expect(parsed.cleaned, `prefix ${JSON.stringify(p)}`).toBe("Answer.");
    }
  });
});

describe("the inferred narration across abbreviations", () => {
  it("keeps a number after nr. in the sentence", () => {
    const { inferredNarration } = stripAndParseNextAction(
      "Am găsit **Decizia nr. 31/2021 – Sandoz**. O citesc acum.",
    );
    expect(inferredNarration).toBe("Am găsit **Decizia nr. 31/2021 – Sandoz**.");
  });

  it("does not end a sentence at art., alin. or lit.", () => {
    const { inferredNarration } = stripAndParseNextAction(
      "Încadrarea este la art. 5 alin. (1) lit. a) din lege. Verific precedentele.",
    );
    expect(inferredNarration).toBe("Încadrarea este la art. 5 alin. (1) lit. a) din lege.");
  });

  it("does not end a sentence at an initial or an abbreviation before a capital", () => {
    const { inferredNarration } = stripAndParseNextAction(
      "Am citit decizia privind S.C. Alfa S.A. și Beta S.R.L. din județ. Urmează raportul.",
    );
    expect(inferredNarration).toBe("Am citit decizia privind S.C. Alfa S.A. și Beta S.R.L. din județ.");
  });

  it("still ends at a plain sentence terminator", () => {
    const { inferredNarration } = stripAndParseNextAction("Caut deciziile. Apoi le citesc.");
    expect(inferredNarration).toBe("Caut deciziile.");
  });
});
