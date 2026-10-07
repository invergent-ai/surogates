// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Surogate Desktop's sign-in, in the system browser: the agent's own sign-in, then the user
// allows the desktop on their computer, and the browser goes back to it there
// (web/src/lib/oauth-consent.ts, surogates/api/routes/oauth.py). Nothing here goes through
// authFetch: a session that cannot refresh would send the browser to /login, and the
// desktop's request with it.
import { useEffect, useMemo, useState } from "react";
import { refreshSession } from "@/api/auth";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { getDesktop } from "@/lib/desktop-bridge";
import { consentRequest, decide, recentSignIn } from "@/lib/oauth-consent";
import { LoginPage } from "./login-page";
import { getAuthToken, hasRefreshToken } from "./session";

const SIGN_IN = "Sign in to continue to Surogate Desktop.";
const SIGN_IN_AGAIN = "Sign in again to continue to Surogate Desktop: allowing it needs a sign-in from the last few minutes.";

type Step =
  | { kind: "checking" }
  | { kind: "sign-in"; notice: string }
  | { kind: "consent"; email: string }
  | { kind: "sent" }
  | { kind: "refused"; message: string };

function Notice({ text }: { text: string }) {
  return (
    <div className="bg-background text-foreground min-h-dvh flex items-center justify-center px-4 text-sm">
      <p className="max-w-[420px] text-center text-muted-foreground">{text}</p>
    </div>
  );
}

export function AuthorizePage() {
  const request = useMemo(() => consentRequest(window.location.search), []);
  const [step, setStep] = useState<Step>({ kind: "checking" });
  const [busy, setBusy] = useState(false);

  // The consent, for whoever is signed in here with a session that still refreshes; else the sign-in.
  const consentOrSignIn = async (notice: string) => {
    const token = hasRefreshToken() && (await refreshSession()) ? getAuthToken() : null;
    // A sign-in too old to allow the desktop asks for a new one first, rather than after Allow.
    if (token && !recentSignIn(token)) {
      setStep({ kind: "sign-in", notice: SIGN_IN_AGAIN });
      return;
    }
    const me = token ? await fetch("/api/v1/auth/me", { headers: { Authorization: `Bearer ${token}` } }).catch(() => null) : null;
    if (!me?.ok) {
      setStep({ kind: "sign-in", notice });
      return;
    }
    setStep({ kind: "consent", email: ((await me.json()) as { email: string }).email });
  };

  useEffect(() => {
    if (!getDesktop()) void consentOrSignIn(SIGN_IN);
  }, []);

  // In Surogate Desktop's own window there is no sign-in form: the app opens this page in the browser.
  if (getDesktop()) return <Notice text="Surogate signs you in from its own window, in your browser." />;
  if (!request) return <Notice text="This sign-in link is not complete. Start signing in again from Surogate Desktop." />;
  if (step.kind === "checking") return <Notice text="Checking your sign-in…" />;
  if (step.kind === "sent") return <Notice text="Finishing in Surogate Desktop…" />;
  if (step.kind === "refused") return <Notice text={step.message} />;
  if (step.kind === "sign-in") {
    const { notice } = step;
    return <LoginPage notice={notice} onSignedIn={() => void consentOrSignIn(notice)} />;
  }

  const answer = async (decision: "allow" | "deny") => {
    setBusy(true);
    const outcome = await decide(request, decision, (body) =>
      fetch("/api/v1/auth/oauth/authorize", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${getAuthToken() ?? ""}` },
        body: JSON.stringify(body),
      }),
    ).catch(() => ({ kind: "refused" as const, message: "The agent could not be reached. Try again from Surogate Desktop." }));
    setBusy(false);
    if (outcome.kind === "redirect") {
      setStep({ kind: "sent" });
      window.location.assign(outcome.to);
    } else if (outcome.kind === "sign-in-again") {
      setStep({ kind: "sign-in", notice: SIGN_IN_AGAIN });
    } else {
      setStep({ kind: "refused", message: outcome.message });
    }
  };

  return (
    <div className="bg-background text-foreground min-h-dvh flex items-center justify-center px-4 text-sm">
      <Card className="w-full max-w-[420px] rounded-2xl border border-line px-6 py-8 sm:px-10">
        <h3 className="text-2xl font-bold tracking-tight">Allow Surogate Desktop?</h3>
        <p>
          Surogate Desktop on <strong>{request.computer}</strong> wants to sign in to {window.location.host} as{" "}
          <strong>{step.email}</strong>.
        </p>
        <p className="text-muted-foreground">
          It can then use this agent for you on that computer, and work on the folders of it you choose there. Allow
          it only if you started signing in from Surogate Desktop just now.
        </p>
        <div className="mt-2 flex gap-2">
          <Button type="button" disabled={busy} onClick={() => void answer("allow")}>
            Allow
          </Button>
          <Button type="button" variant="outline" disabled={busy} onClick={() => void answer("deny")}>
            Deny
          </Button>
        </div>
      </Card>
    </div>
  );
}
