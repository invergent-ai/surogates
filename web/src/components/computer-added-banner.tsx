// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// "<computer> can now work on folders of your computer through <agent>", in every web client of
// the user's for 7 days after a computer is added or reauthorized. Dismissed in this browser
// only: whoever added a computer cannot hide the notice from the user's other clients.
import { LaptopIcon, XIcon } from "lucide-react";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { type Device, listDevices } from "@/api/devices";
import { addedNotice, addedNotices, dismissedNotices, dismissNotice } from "@/lib/devices";

// Storage can be refused (a private window, blocked site data): the notice still shows, and still goes for the page.
const storage = (): Storage | null => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

export function ComputerAddedBanner() {
  const navigate = useNavigate();
  const [devices, setDevices] = useState<Device[]>([]);
  const [dismissed, setDismissed] = useState(() => dismissedNotices(storage()));

  // ponytail: read once per page shown; a computer added meanwhile shows at the next page.
  useEffect(() => {
    let live = true;
    void listDevices().then((rows) => {
      if (live) setDevices(rows);
    }, () => {});
    return () => {
      live = false;
    };
  }, []);

  const notices = addedNotices(devices, new Date(), dismissed);
  if (notices.length === 0) return null;
  return (
    <div className="shrink-0 border-b border-line bg-card">
      {notices.map(({ key, name }) => (
        <div key={key} role="status" className="flex items-center gap-2 px-4 py-2 text-sm">
          <LaptopIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0 flex-1">{addedNotice(name, window.location.host)}</span>
          {/* Where the user checks the computer, and revokes it if it is not theirs. */}
          <button
            type="button"
            className="shrink-0 rounded-md px-2 py-1 font-medium text-foreground hover:bg-input"
            onClick={() => void navigate({ to: "/settings", search: { tab: "devices" } })}
          >
            Review
          </button>
          <button
            type="button"
            aria-label="Dismiss"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-input hover:text-foreground"
            onClick={() => {
              // Kept only while it is told: the dismissals never outgrow the notices.
              dismissNotice(storage(), key, addedNotices(devices, new Date(), new Set()).map((notice) => notice.key));
              setDismissed(new Set([...dismissed, key]));
            }}
          >
            <XIcon className="size-4" />
          </button>
        </div>
      ))}
    </div>
  );
}
