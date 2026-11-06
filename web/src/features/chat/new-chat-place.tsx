// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Under a new chat's composer, in Surogate Desktop: where the chat will work (Section 8).
import type { DesktopFolderChoice } from "@/lib/desktop-bridge";
import { FOLDER_CHOICES, FOLDER_NOTE } from "@/lib/local-chat";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@invergent/ui-sdk/dropdown-menu";
import { TextLink } from "@invergent/ui-sdk/text-link";
import {
  ChevronDownIcon,
  CloudIcon,
  FolderOpenIcon,
  FolderPlusIcon,
  HistoryIcon,
  LaptopIcon,
} from "lucide-react";

const CHOICE_ICONS = {
  new: FolderPlusIcon,
  last: HistoryIcon,
  pick: FolderOpenIcon,
} as const;

export function NewChatPlace({
  place,
  choice,
  menu,
  onChoice,
}: {
  place: { local: boolean; text: string };
  choice: DesktopFolderChoice;
  menu: boolean;
  onChoice: (choice: DesktopFolderChoice) => void;
}) {
  const Icon = place.local ? LaptopIcon : CloudIcon;
  const chosen = FOLDER_CHOICES.find((c) => c.value === choice);
  const ChosenIcon = CHOICE_ICONS[choice];
  return (
    <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 px-1 text-xs text-muted-foreground">
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      <span>{place.text}</span>
      {place.local && menu && (
        <>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                aria-label={`${place.text} ${chosen?.shown ?? ""}`.trim()}
                className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-medium text-foreground hover:bg-muted"
              >
                <ChosenIcon className="size-3.5" aria-hidden="true" />
                {chosen?.shown}
                <ChevronDownIcon className="size-3.5" aria-hidden="true" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent side="top" align="start">
              <DropdownMenuRadioGroup
                value={choice}
                onValueChange={(value) =>
                  onChoice(value as DesktopFolderChoice)
                }
              >
                {FOLDER_CHOICES.map(({ value, label, hint }) => {
                  const ItemIcon = CHOICE_ICONS[value];
                  return (
                    <DropdownMenuRadioItem key={value} value={value}>
                      <ItemIcon className="size-4" aria-hidden="true" />
                      <span className="flex flex-col">
                        <span>{label}</span>
                        <span className="text-xs text-muted-foreground">
                          {hint}
                        </span>
                      </span>
                    </DropdownMenuRadioItem>
                  );
                })}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <span aria-hidden="true">·</span>
          <span>{FOLDER_NOTE}</span>
        </>
      )}
      {place.local && !menu && (
        // The desktop's own picker opens when the message is sent: the page names no folder before then.
        <TextLink asChild size="inline">
          <button
            type="button"
            onClick={() => onChoice(choice === "last" ? "pick" : "last")}
          >
            {choice === "last"
              ? "Choose another folder"
              : "Use the last folder"}
          </button>
        </TextLink>
      )}
    </p>
  );
}
