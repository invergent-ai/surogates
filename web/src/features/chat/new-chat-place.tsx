// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Under a new chat's composer, in Surogate Desktop: where the chat will work (Section 8).
import { CloudIcon, LaptopIcon } from "lucide-react";

export function NewChatPlace({
  place,
  choice,
  onChoice,
}: {
  place: { local: boolean; text: string };
  choice: "last" | "pick";
  onChoice: (choice: "last" | "pick") => void;
}) {
  const Icon = place.local ? LaptopIcon : CloudIcon;
  return (
    <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 px-1 text-xs text-muted-foreground">
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      <span>{place.text}</span>
      {place.local && (
        // The desktop's own picker opens when the message is sent: the page names no folder before then.
        <button
          type="button"
          className="font-medium text-foreground underline-offset-2 hover:underline"
          onClick={() => onChoice(choice === "last" ? "pick" : "last")}
        >
          {choice === "last" ? "Choose another folder" : "Use the last folder"}
        </button>
      )}
    </p>
  );
}
