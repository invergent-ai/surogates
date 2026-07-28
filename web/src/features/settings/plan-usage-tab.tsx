// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Settings → Plan & usage: what this agent sells and what the
// signed-in user currently holds. Balances arrive token-denominated
// from the harness's /v1/commerce endpoints (which front surogate-ops
// with the runtime token) and are shown as messages; the browser
// never talks to ops directly.

import { BrandBeam } from "@invergent/agent-chat-react";
import { useCallback, useEffect, useState } from "react";
import { authFetch } from "@/api/auth";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { approxMessagesLabel } from "@/lib/usage-units";

interface CommerceOffer {
  id: string;
  kind: "subscription" | "token_pack";
  name: string;
  currency: string;
  amount_cents: number;
  billing_interval: string | null;
  token_amount: number;
  /** Buyer-facing labels for a custom package; [] or absent = full
   * access (subscriptions) / pure extra usage (packs). */
  included?: string[];
}

interface CommerceOverview {
  mode: string;
  buy_url: string | null;
  offers: CommerceOffer[];
  entitlement: {
    subscription_status: string;
    current_period_end: string | null;
    period_token_remaining: number;
    topup_token_remaining: number;
    included?: string[];
  } | null;
  purchasable: boolean;
}

const ACTIVE_SUB = new Set(["active", "trialing"]);

const errorMessage = (e: unknown, fallback: string): string =>
  e instanceof Error ? e.message : fallback;

function formatPrice(amountCents: number, currency: string): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: currency.toUpperCase(),
    minimumFractionDigits: amountCents % 100 === 0 ? 0 : 2,
  }).format(amountCents / 100);
}

export function PlanUsageTab() {
  const [overview, setOverview] = useState<CommerceOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyOfferId, setBusyOfferId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await authFetch("/api/v1/commerce/overview");
      if (!r.ok) {
        throw new Error("Plan information is temporarily unavailable.");
      }
      setOverview((await r.json()) as CommerceOverview);
    } catch (e) {
      setError(
        errorMessage(e, "Plan information is temporarily unavailable."),
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const buy = async (offerId: string) => {
    setBusyOfferId(offerId);
    setError(null);
    try {
      const r = await authFetch("/api/v1/commerce/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offer_id: offerId }),
      });
      const body = (await r.json().catch(() => null)) as {
        url?: string;
        detail?: string;
      } | null;
      if (!r.ok || !body?.url) {
        throw new Error(body?.detail ?? "Checkout is unavailable right now.");
      }
      window.location.assign(body.url);
    } catch (e) {
      setError(errorMessage(e, "Checkout is unavailable right now."));
      setBusyOfferId(null);
    }
  };

  if (error && overview === null) {
    return (
      <Alert variant="destructive" data-testid="plan-tab-error">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }
  if (overview === null) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="plan-tab-loading">
        Loading…
      </p>
    );
  }
  if (overview.mode === "free") {
    return (
      <p className="text-sm text-muted-foreground" data-testid="plan-tab-free">
        This agent is free to use. There is nothing to buy.
      </p>
    );
  }

  const ent = overview.entitlement;
  const subscriptions = overview.offers.filter(
    (o) => o.kind === "subscription",
  );
  const packs = overview.offers.filter((o) => o.kind === "token_pack");

  return (
    <div className="flex max-w-[680px] flex-col gap-6" data-testid="plan-tab">
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* ── Current balance ── */}
      <Card data-testid="plan-tab-balance">
        <CardHeader>
          <CardTitle>Your access</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm">
          {ent === null ? (
            <p className="text-muted-foreground">
              Purchases aren't available for this account — it was created
              by the agent's operator, so your access is managed for you.
            </p>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Subscription</span>
                <span className="font-medium">
                  {ACTIVE_SUB.has(ent.subscription_status)
                    ? `Active${
                        ent.current_period_end
                          ? ` · renews ${new Date(
                              ent.current_period_end,
                            ).toLocaleDateString()}`
                          : ""
                      }`
                    : "None"}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">
                  Included usage left this period
                </span>
                <span className="font-medium tabular-nums">
                  {approxMessagesLabel(ent.period_token_remaining)}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Extra usage</span>
                <span className="font-medium tabular-nums">
                  {approxMessagesLabel(ent.topup_token_remaining)}
                </span>
              </div>
              {(ent.included?.length ?? 0) > 0 ? (
                <div
                  className="flex items-start justify-between gap-4"
                  data-testid="plan-tab-included"
                >
                  <span className="shrink-0 text-muted-foreground">
                    Your plan includes
                  </span>
                  <span className="text-right font-medium">
                    {ent.included?.join(" · ")}
                  </span>
                </div>
              ) : null}
            </>
          )}
        </CardContent>
      </Card>

      {/* ── Offers ── */}
      {[
        // highlightFirst: the lead plan is the recommended one and gets
        // the brand beam; selection is a data flag, not display copy.
        { label: "Plans", items: subscriptions, verb: "Subscribe", highlightFirst: true },
        { label: "Extra usage", items: packs, verb: "Buy", highlightFirst: false },
      ]
        .filter((s) => s.items.length > 0)
        .map((section) => (
          <div key={section.label} className="flex flex-col gap-2.5">
            <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
              {section.label}
            </p>
            {section.items.map((offer, offerIndex) => (
              <BrandBeam
                key={offer.id}
                size="md"
                strength={0.55}
                borderRadius={8}
                active={section.highlightFirst && offerIndex === 0}
              >
                <div
                  data-testid={`plan-offer-${offer.id}`}
                  className="flex items-center gap-4 rounded-lg border border-border bg-card px-4 py-3"
                >
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span className="text-sm font-medium">{offer.name}</span>
                    <span className="text-xs text-muted-foreground">
                      {approxMessagesLabel(offer.token_amount)}
                      {offer.kind === "subscription"
                        ? " every period"
                        : " of extra usage · one-time"}
                    </span>
                    {(offer.included?.length ?? 0) > 0 ? (
                      <span className="text-xs text-muted-foreground/80">
                        Includes: {offer.included?.join(" · ")}
                      </span>
                    ) : null}
                  </div>
                  <span className="shrink-0 text-sm font-semibold tabular-nums">
                    {formatPrice(offer.amount_cents, offer.currency)}
                    {offer.kind === "subscription" && offer.billing_interval
                      ? `/${offer.billing_interval}`
                      : ""}
                  </span>
                  <Button
                    size="sm"
                    disabled={!overview.purchasable || busyOfferId !== null}
                    onClick={() => void buy(offer.id)}
                  >
                    {busyOfferId === offer.id ? "Redirecting…" : section.verb}
                  </Button>
                </div>
              </BrandBeam>
            ))}
          </div>
        ))}
    </div>
  );
}
