// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A quiet offer to be notified when the agent writes, shown only once the
// person has written something themselves (never on a first load), and
// never again for a while after "Not now".

import { Button } from "@/components/ui/button";
import {
  type PushState,
  dismissOffer,
  enablePush,
  offerDismissed,
  pushState,
} from "@/lib/web-push";
import { useEffect, useState } from "react";

export function NotifyOptIn({ engaged }: { engaged: boolean }) {
  const [state, setState] = useState<PushState | null>(null);
  const [hidden, setHidden] = useState(offerDismissed);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!engaged || hidden) {
      return;
    }
    let live = true;
    void pushState().then((s) => {
      if (live) {
        setState(s);
      }
    });
    return () => {
      live = false;
    };
  }, [engaged, hidden]);

  if (!engaged || hidden || state !== "off") {
    return null;
  }

  return (
    <div
      aria-live="polite"
      className="mx-auto mt-2 flex w-full max-w-3xl items-center gap-3 rounded-lg border bg-muted/40 px-3 py-2 text-sm"
    >
      <span className="flex-1 text-muted-foreground">
        Get notified when the agent writes to you, even with this page closed.
      </span>
      <Button
        size="sm"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          enablePush()
            .then(setState, () => setState("off"))
            .finally(() => setBusy(false));
        }}
      >
        Notify me
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          dismissOffer();
          setHidden(true);
        }}
      >
        Not now
      </Button>
    </div>
  );
}
