// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Browser notifications for this agent, on or off, in this browser.

import { BellIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Switch } from "@invergent/ui-sdk/switch";
import {
  type PushState,
  disablePush,
  enablePush,
  pushState,
} from "@/lib/web-push";

export function NotificationsSection() {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void pushState().then(setState);
  }, []);

  if (state === null || state === "unsupported") {
    return null;
  }

  const change = (on: boolean) => {
    setBusy(true);
    (on ? enablePush() : disablePush())
      .then(setState, () => toast.error("Could not change notifications."))
      .finally(() => setBusy(false));
  };

  return (
    <div className="border-t border-line pt-5 mt-6">
      <div className="flex items-center gap-2 mb-3">
        <BellIcon className="w-4 h-4 text-muted-foreground" />
        <h2 className="text-sm font-semibold text-foreground">Notifications</h2>
      </div>
      <label className="flex items-center gap-3 text-sm">
        <Switch
          checked={state === "on"}
          disabled={busy || state === "denied"}
          onCheckedChange={change}
        />
        <span className="text-muted-foreground">
          {state === "denied"
            ? "Notifications are blocked for this site in your browser's settings."
            : "Notify me in this browser when the agent writes to me."}
        </span>
      </label>
    </div>
  );
}
