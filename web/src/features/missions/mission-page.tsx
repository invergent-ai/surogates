// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Mission dashboard — fleshed out by Task 14. This stub exists so the
// route registered in `app/routes/missions.tsx` resolves cleanly during
// Task 13's typecheck.
import { useParams } from "@tanstack/react-router";

export function MissionPage() {
  const { missionId } = useParams({ strict: false }) as {
    missionId: string | undefined;
  };
  return (
    <div className="p-6">
      <h1 className="text-2xl font-semibold">Mission</h1>
      <p className="text-sm text-muted-foreground mt-2">
        Loading dashboard for mission {missionId ?? "(unknown)"}…
      </p>
    </div>
  );
}
