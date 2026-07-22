// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Settings → Plan & tokens: what this agent sells and what the
// signed-in user currently holds. Data comes from the harness's
// /v1/commerce endpoints (which front surogate-ops with the runtime
// token) — the browser never talks to ops directly.

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

interface CommerceOffer {
  id: string;
  kind: "subscription" | "token_pack";
  name: string;
  currency: string;
  amount_cents: number;
  billing_interval: string | null;
  token_amount: number;
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
  } | null;
  purchasable: boolean;
}

const ACTIVE_SUB = new Set(["active", "trialing"]);

function formatPrice(amountCents: number, currency: string): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: currency.toUpperCase(),
    minimumFractionDigits: amountCents % 100 === 0 ? 0 : 2,
  }).format(amountCents / 100);
}

function formatTokens(n: number): string {
  return n.toLocaleString("en-US");
}

export function PlanTokensTab() {
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
        e instanceof Error
          ? e.message
          : "Plan information is temporarily unavailable.",
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
      setError(
        e instanceof Error ? e.message : "Checkout is unavailable right now.",
      );
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
        This agent is free to use — there are no plans or token packs.
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
                  Plan tokens this period
                </span>
                <span className="font-medium tabular-nums">
                  {formatTokens(ent.period_token_remaining)}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Top-up tokens</span>
                <span className="font-medium tabular-nums">
                  {formatTokens(ent.topup_token_remaining)}
                </span>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {/* ── Offers ── */}
      {[
        { label: "Plans", items: subscriptions, verb: "Subscribe" },
        { label: "Token packs", items: packs, verb: "Buy" },
      ]
        .filter((s) => s.items.length > 0)
        .map((section) => (
          <div key={section.label} className="flex flex-col gap-2.5">
            <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
              {section.label}
            </p>
            {section.items.map((offer) => (
              <div
                key={offer.id}
                data-testid={`plan-offer-${offer.id}`}
                className="flex items-center gap-4 rounded-lg border border-border bg-card px-4 py-3"
              >
                <div className="flex min-w-0 flex-1 flex-col">
                  <span className="text-sm font-medium">{offer.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {formatTokens(offer.token_amount)} tokens
                    {offer.kind === "subscription"
                      ? " every period"
                      : " · one-time"}
                  </span>
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
            ))}
          </div>
        ))}
    </div>
  );
}
