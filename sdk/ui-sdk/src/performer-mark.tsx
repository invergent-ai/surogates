// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Who does a piece of work, as an icon in a round pill. Agents wear the info
// tone and every other performer stays muted: the icon tells the kinds
// apart, which keeps amber for a page's one primary action.
import {
  BotIcon,
  Building2Icon,
  type LucideIcon,
  PlugIcon,
  ScaleIcon,
  UserIcon,
  UserRoundIcon,
  UsersIcon,
} from "lucide-react";

import { type Tone, TonePill } from "./chip";
import { cn } from "./lib/utils";

export type PerformerKind =
  | "person"
  | "agent"
  | "agent_with_person"
  | "system"
  | "rule"
  | "client"
  | "third_party"
  | "queue";

const PERFORMERS: Record<
  PerformerKind,
  { word: string; icon: LucideIcon; tone: Tone }
> = {
  person: { word: "Person", icon: UserIcon, tone: "muted" },
  agent: { word: "Agent", icon: BotIcon, tone: "info" },
  agent_with_person: {
    word: "Agent, finished by a person",
    icon: BotIcon,
    tone: "info",
  },
  system: { word: "System", icon: PlugIcon, tone: "muted" },
  rule: { word: "Rule", icon: ScaleIcon, tone: "muted" },
  client: { word: "Client", icon: UserRoundIcon, tone: "muted" },
  third_party: { word: "Third party", icon: Building2Icon, tone: "muted" },
  queue: { word: "Queue", icon: UsersIcon, tone: "muted" },
};

/** The performer in words, as a sentence-case name: "Third party". */
export function performerWord(kind: PerformerKind): string {
  return PERFORMERS[kind].word;
}

/** The performer as an icon, named for screen readers and on hover. */
export function PerformerMark({
  kind,
  className,
}: {
  kind: PerformerKind;
  className?: string;
}) {
  const { word, icon: Icon, tone } = PERFORMERS[kind];
  return (
    <TonePill
      tone={tone}
      role="img"
      aria-label={word}
      title={word}
      className={cn("px-1.5", className)}
    >
      <Icon className="size-3" />
    </TonePill>
  );
}
