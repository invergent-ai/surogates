// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// EU AI Act Art. 13/50 transparency disclosure banner.
// Shown when a new session is created. The user must accept before
// the agent can execute tools. Declining disables the session.
//
import { useState } from "react";
import { ShieldCheckIcon } from "lucide-react";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import * as sessionsApi from "@/api/sessions";

type TransparencyLevel = "none" | "basic" | "enhanced" | "full";

const DISCLOSURE_TEXT: Record<TransparencyLevel, { body: string; legal: string }> = {
  none: {
    body: "",
    legal: "",
  },
  basic: {
    body:
      "You are about to interact with an AI system. All outputs are " +
      "machine-generated and may contain errors.",
    legal:
      "In accordance with the EU AI Act (Art. 50(1)), you are being " +
      "informed that this system uses artificial intelligence to process " +
      "your requests.",
  },
  enhanced: {
    body:
      "You are about to interact with a high-risk AI system. All outputs " +
      "are machine-generated, subject to governance policy enforcement, " +
      "and logged for regulatory audit purposes. Interpretability " +
      "documentation is available on request.",
    legal:
      "In accordance with the EU AI Act (Art. 13, Art. 50(1)), you are " +
      "being informed that this system uses artificial intelligence. " +
      "This system is classified as high-risk and is subject to " +
      "transparency and oversight obligations.",
  },
  full: {
    body:
      "You are about to interact with a high-risk AI system under full " +
      "transparency obligations. All tool calls are policy-governed, " +
      "audited, and subject to human oversight. System accuracy " +
      "declarations and technical documentation are available.",
    legal:
      "In accordance with the EU AI Act (Art. 13, Art. 14, Art. 50), " +
      "you are being informed that this system uses artificial " +
      "intelligence under full transparency, interpretability, and " +
      "human oversight requirements. Detailed technical documentation " +
      "and accuracy declarations are available on request.",
  },
};

interface TransparencyBannerProps {
  sessionId: string;
  level: TransparencyLevel;
  onConfirmed: () => void;
  onDeclined: () => void;
}

export function TransparencyBanner({
  sessionId,
  level,
  onConfirmed,
  onDeclined,
}: TransparencyBannerProps) {
  const [confirming, setConfirming] = useState(false);

  const texts = DISCLOSURE_TEXT[level] || DISCLOSURE_TEXT.basic;

  const handleAccept = async () => {
    setConfirming(true);
    try {
      await sessionsApi.confirmDisclosure(sessionId);
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
