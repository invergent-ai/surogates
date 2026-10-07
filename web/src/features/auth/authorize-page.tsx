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
const UNREACHABLE = "Signed in, but the agent could not be reached. Try again.";

type Step =
  | { kind: "checking" }
  | { kind: "sign-in"; notice: string }
  | { kind: "consent"; email: string }
  | { kind: "sent" }
  | { kind: "refused"; message: string };

// The session's access token, refreshed just now so its `iat` is the agent's now; null when it cannot refresh.
async function freshToken(): Promise<string | null> {
  return hasRefreshToken() && (await refreshSession()) ? getAuthToken() : null;
}

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
  const consentOrSignIn = async (notice: string): Promise<Step> => {
    const token = await freshToken();
    if (!token) return { kind: "sign-in", notice };
    // A sign-in too old to allow the desktop asks for a new one first, rather than after Allow.
    if (!recentSignIn(token)) return { kind: "sign-in", notice: SIGN_IN_AGAIN };
    const me = await fetch("/api/v1/auth/me", { headers: { Authorization: `Bearer ${token}` } }).catch(() => null);
    const email = me?.ok ? ((await me.json().catch(() => null)) as { email?: unknown } | null)?.email : undefined;
    return typeof email === "string" ? { kind: "consent", email } : { kind: "sign-in", notice: UNREACHABLE };
  };

  useEffect(() => {
    if (!getDesktop()) void consentOrSignIn(SIGN_IN).then(setStep);
  }, []);

  // In Surogate Desktop's own window there is no sign-in form: the app opens this page in the browser.
  if (getDesktop()) return <Notice text="Surogate signs you in from its own window, in your browser." />;
  if (!request) return <Notice text="This sign-in link is not complete. Start signing in again from Surogate Desktop." />;
  if (step.kind === "checking") return <Notice text="Checking your sign-in…" />;
  if (step.kind === "sent") return <Notice text="Finishing in Surogate Desktop…" />;
  if (step.kind === "refused") return <Notice text={step.message} />;
  if (step.kind === "sign-in") {
    const { notice } = step;
    return <LoginPage notice={notice} onSignedIn={() => void consentOrSignIn(notice).then(setStep)} />;
  }

  const answer = async (decision: "allow" | "deny") => {
    setBusy(true);
    // The sign-in may have aged past recent while the user read the consent: ask again now, not after Allow.
    const token = decision === "allow" ? await freshToken() : null;
    if (decision === "allow" && !recentSignIn(token)) {
      setBusy(false);
      // A refresh the network failed keeps the session: the agent could not be reached, and a new sign-in would not help.
      setStep({ kind: "sign-in", notice: token === null && hasRefreshToken() ? UNREACHABLE : SIGN_IN_AGAIN });
      return;
    }
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
          Surogate Desktop on{" "}
          <strong>
            {/* Isolated and cut to its own line box: no name the request carries can reorder or cover the rest. */}
            <bdi className="inline-block max-w-full truncate align-bottom">{request.computer}</bdi>
          </strong>{" "}
          wants to sign in to {window.location.host} as{" "}
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
