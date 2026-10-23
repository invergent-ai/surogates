// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The marks a row of work carries: who does the work, and where it stands
// against its service level. Every date is built in local time, so the
// readings hold in any time zone.
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  type PerformerKind,
  PerformerMark,
  performerWord,
} from "../src/performer-mark";
import { formatDuration, ServiceLevelPill } from "../src/service-level-pill";
import { cleanup, render } from "./dom";

afterEach(cleanup);

const PERFORMERS: [PerformerKind, string, string][] = [
  ["person", "Person", "muted"],
  ["agent", "Agent", "info"],
  ["agent_with_person", "Agent, finished by a person", "info"],
  ["system", "System", "muted"],
  ["rule", "Rule", "muted"],
  ["client", "Client", "muted"],
  ["third_party", "Third party", "muted"],
  ["queue", "Queue", "muted"],
];

function marks(): HTMLElement[] {
  const view = render(
    <>
      {PERFORMERS.map(([kind]) => (
        <PerformerMark key={kind} kind={kind} />
      ))}
    </>,
  );
  return Array.from(view.querySelectorAll<HTMLElement>("[role=img]"));
}

describe("performer marks", () => {
  it("a performer mark names its kind", () => {
    const shown = marks();
    const words = PERFORMERS.map(([, word]) => word);

    expect(shown.map((m) => m.getAttribute("aria-label"))).toEqual(words);
    expect(shown.map((m) => m.getAttribute("title"))).toEqual(words);
    expect(PERFORMERS.map(([kind]) => performerWord(kind))).toEqual(words);
    expect(shown[1].querySelector("svg.lucide-bot")).not.toBeNull();
    expect(shown[2].querySelector("svg.lucide-bot")).not.toBeNull();
  });

  it("only agents wear a colour; every other performer is muted", () => {
    expect(marks().map((m) => m.getAttribute("data-tone"))).toEqual(
      PERFORMERS.map(([, , tone]) => tone),
    );
  });

  it("each kind draws its own icon, an agent finished by a person drawing the agent's", () => {
    const drawings = marks().map((m) => {
      const icons = m.querySelectorAll("svg");
      expect(icons).toHaveLength(1);
      return icons[0].innerHTML;
    });

    expect(drawings[2]).toBe(drawings[1]);
    const others = drawings.filter((_, i) => i !== 2);
    expect(new Set(others).size).toBe(others.length);
  });
});

type PillProps = ComponentProps<typeof ServiceLevelPill>;

function reading(props: PillProps): [string | null, string | null] {
  const pill = render(<ServiceLevelPill {...props} />).firstElementChild;
  return [pill?.textContent ?? null, pill?.getAttribute("data-tone") ?? null];
}

const late = new Date(2026, 9, 23, 22, 0);
const morning = new Date(2026, 9, 23, 9, 0);

describe("service level pills", () => {
  it("a duration reads in its largest whole unit, rounded down", () => {
    const minute = 60_000;
    const hour = 60 * minute;
    const day = 24 * hour;

    expect(formatDuration(0)).toBe("1 min");
    expect(formatDuration(59_999)).toBe("1 min");
    expect(formatDuration(hour - 1)).toBe("59 min");
    expect(formatDuration(hour)).toBe("1 h");
    expect(formatDuration(day - 1)).toBe("23 h");
    expect(formatDuration(day)).toBe("1 d");
    expect(formatDuration(4 * day - 1)).toBe("3 d");
  });

  it("the service level reads time left, due today, past goal and past deadline", () => {
    const cases: [PillProps, string, string][] = [
      [{ state: "ok", deadlineAt: new Date(2026, 9, 24, 1, 0), now: late }, "3 h left", "muted"],
      [{ state: "ok", deadlineAt: new Date(2026, 9, 23, 17, 0).toISOString(), now: morning }, "Due today", "warn"],
      [{ state: "ok", deadlineAt: new Date(2026, 9, 26, 9, 0), now: morning }, "3 d left", "muted"],
      [{ state: "past-goal", deadlineAt: new Date(2026, 9, 26, 9, 0), now: morning }, "Past goal", "warn"],
      [{ state: "past-deadline", deadlineAt: new Date(2026, 9, 22, 7, 0), now: morning }, "Past deadline · 1 d", "bad"],
      [{ state: "ok", now: morning }, "On time", "muted"],
    ];

    for (const [props, words, tone] of cases) {
      expect(reading(props)).toEqual([words, tone]);
    }
  });

  it("the service level follows the state it is given, whatever this clock says", () => {
    expect(
      reading({ state: "ok", deadlineAt: new Date(2026, 9, 21, 9, 0), now: morning }),
    ).toEqual(["On time", "muted"]);
    expect(
      reading({ state: "past-deadline", deadlineAt: new Date(2026, 9, 25, 9, 0), now: morning }),
    ).toEqual(["Past deadline", "bad"]);
    expect(reading({ state: "past-deadline", now: morning })).toEqual([
      "Past deadline",
      "bad",
    ]);
    expect(reading({ state: "past-goal", now: morning })).toEqual([
      "Past goal",
      "warn",
    ]);
  });

  it("without a clock the service level reads against the time of render", () => {
    const day = 24 * 60 * 60_000;
    const deadlineAt = new Date(Date.now() + 2.5 * day);

    expect(reading({ state: "ok", deadlineAt })).toEqual(["2 d left", "muted"]);
  });

  it("a paused service level says it is paused, in the words it is given", () => {
    const deadlineAt = new Date(2026, 9, 22, 7, 0);

    expect(
      reading({ state: "past-deadline", deadlineAt, now: morning, paused: true }),
    ).toEqual(["Paused", "info"]);
    expect(
      reading({
        state: "past-deadline",
        deadlineAt,
        now: morning,
        paused: true,
        pausedLabel: "Waiting on the client",
      }),
    ).toEqual(["Waiting on the client", "info"]);
  });
});
