// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// EU AI Act Art. 13/50 transparency disclosure banner.
// Shown on the landing screen (before any session exists) or when a
// new session has zero messages. The user must accept before the
// agent can execute tools. Declining disables interaction.
//
import { useState } from "react";
import { ShieldCheckIcon } from "lucide-react";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import * as sessionsApi from "@/api/sessions";

type TransparencyLevel = "none" | "basic" | "enhanced" | "full";

// Fallback copies when the server did not send its disclosure text
// (older runtimes). Deliberately does NOT self-classify the system as
// "high-risk" — that is an AI Act Art. 6 legal classification these
// agents do not carry; the levels scale detail, not risk claims. Keep
// in sync with surogates/governance/transparency.py DISCLOSURE_TEXTS.
const DISCLOSURE_TEXT: Record<TransparencyLevel, { body: string; legal: string }> = {
  none: {
    body: "",
    legal: "",
  },
  basic: {
    body:
      "You are about to interact with an AI assistant. Replies are " +
      "machine-generated and may contain errors.",
    legal:
      "In accordance with the EU AI Act (Art. 50(1)), you are being " +
      "informed that this system uses artificial intelligence to process " +
      "your requests.",
  },
  enhanced: {
    body:
      "You are about to interact with an AI assistant. Replies are " +
      "machine-generated, may contain errors, and are logged. The AI " +
      "follows the operator's usage policy and you can always ask for " +
      "a human contact.",
    legal:
      "In accordance with the EU AI Act (Art. 50(1)), you are being " +
      "informed that this system uses artificial intelligence and that " +
      "your conversation is logged.",
  },
  full: {
    body:
      "You are about to interact with an AI assistant operated on the " +
      "Surogate platform. Replies are machine-generated, may contain " +
      "errors, and are logged and policy-governed; a human operator " +
      "reviews escalations and you can request a human contact at any " +
      "time.",
    legal:
      "This notice is provided under EU AI Act Art. 50(1). Further " +
      "information about the system and its operator is available on " +
      "request.",
  },
};

interface TransparencyBannerProps {
  sessionId?: string;
  level: TransparencyLevel;
  // Per-agent disclosure text from the transparency endpoint; falls
  // back to the local level copies when absent (older runtimes).
  serverText?: string;
  onConfirmed: () => void;
  onDeclined: () => void;
}

export function TransparencyBanner({
  sessionId,
  level,
  serverText,
  onConfirmed,
  onDeclined,
}: TransparencyBannerProps) {
  const [confirming, setConfirming] = useState(false);

  const fallback = DISCLOSURE_TEXT[level] || DISCLOSURE_TEXT.basic;
  const texts = serverText
    ? { body: serverText, legal: fallback.legal }
    : fallback;

  const handleAccept = async () => {
    setConfirming(true);
    try {
      // When no session exists yet (pre-session state), accept locally.
      // The backend confirmation is deferred until the session is created.
      if (sessionId) {
        await sessionsApi.confirmDisclosure(sessionId);
      }
      onConfirmed();
    } catch (err) {
      console.error("Failed to confirm disclosure:", err);
      setConfirming(false);
    }
  };

  return (
    <Alert className="w-full max-w-2xl border-primary/30 bg-primary/5 shadow-lg">
      <ShieldCheckIcon className="text-primary" />
      <AlertTitle className="text-base font-semibold">
        AI System Disclosure
      </AlertTitle>
      <AlertDescription className="mt-2 space-y-3">
        <p>{texts.body}</p>
        <p className="text-xs text-muted-foreground">{texts.legal}</p>
        <div className="flex items-center gap-2 pt-1">
          <Button
            size="sm"
            onClick={handleAccept}
            disabled={confirming}
          >
            {confirming ? "Confirming..." : "I understand and accept"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onDeclined}
            disabled={confirming}
          >
            Decline
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}
