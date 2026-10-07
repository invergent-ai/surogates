// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The sign-in page in Surogate Desktop's window: the app signs in itself, in the system
// browser, and gives this page a one-time code for a session of its own. There is no form here.
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { exchangeWebCode } from "@/api/auth";
import { getDesktop, signInFromDesktop } from "@/lib/desktop-bridge";
import { getPostAuthRoute, storeAuthTokens } from "./session";

export function DesktopSignIn() {
  const navigate = useNavigate();
  const [said, setSaid] = useState("Signing you in…");

  useEffect(() => {
    const desktop = getDesktop();
    if (!desktop) return;
    let gone = false;
    signInFromDesktop(desktop, exchangeWebCode, storeAuthTokens).then(
      (signedIn) => {
        if (gone) return;
        if (signedIn) void navigate({ to: getPostAuthRoute() });
        else setSaid("Sign in from Surogate's window.");
      },
      (error: unknown) => {
        if (!gone) setSaid(`Signing in failed: ${error instanceof Error ? error.message : String(error)}`);
      },
    );
    return () => {
      gone = true;
    };
  }, [navigate]);

  return (
    <div className="bg-background text-muted-foreground min-h-dvh flex items-center justify-center px-4 text-sm">
      {said}
    </div>
  );
}
