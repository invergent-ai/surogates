// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Self-registration page for linking messaging platform accounts
// (Slack, Teams, Telegram) to a Surogates user account.

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearch } from "@tanstack/react-router";
import { useTheme } from "next-themes";
import { SunIcon, MoonIcon, Loader2Icon } from "lucide-react";
import { cn } from "@/lib/utils";
import { authFetch } from "@/api/auth";
import { Button } from "@/components/ui/button";
import {
  InputOTP,
  InputOTPGroup,
  InputOTPSlot,
  InputOTPSeparator,
} from "@/components/ui/input-otp";

interface PairingInfo {
  platform: string;
  platform_user_id: string;
  valid: boolean;
}

const PLATFORM_LABELS: Record<string, string> = {
  slack: "Slack",
  teams: "Microsoft Teams",
  telegram: "Telegram",
};

const TAGS = [
  { label: "Managed Agents", icon: "⬡" },
  { label: "MCP Integration", icon: "⚡" },
  { label: "Tool Governance", icon: "◈" },
  { label: "Multi-Tenant", icon: "◇" },
  { label: "K8s Native", icon: "☁" },
] as const;

export function LinkChannelPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { resolvedTheme, setTheme } = useTheme();

  const { code } = useSearch({ strict: false }) as { code?: string };
  // OTP input stores raw chars (no dash); formatted code adds the dash.
  const [rawCode, setRawCode] = useState(() =>
    (code ?? "").replace(/-/g, ""),
  );
  const inputCode = rawCode.length >= 5
    ? `${rawCode.slice(0, 4)}-${rawCode.slice(4)}`
    : rawCode;
  const [pairingInfo, setPairingInfo] = useState<PairingInfo | null>(null);
  const [status, setStatus] = useState<
    "idle" | "loading" | "success" | "error"
  >("idle");
  const [error, setError] = useState("");

  /* ── animated grid background ── */
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let animId: number;
    let time = 0;

    const resize = () => {
      canvas.width = canvas.offsetWidth * 2;
      canvas.height = canvas.offsetHeight * 2;
      ctx.scale(2, 2);
    };
    resize();
    window.addEventListener("resize", resize);

    const draw = () => {
      const w = canvas.offsetWidth;
      const h = canvas.offsetHeight;
      ctx.clearRect(0, 0, w, h);

      const dark = document.documentElement.classList.contains("dark");
      const am = dark ? 1 : 2.5;
      const gridSize = 40;
      const cols = Math.ceil(w / gridSize) + 1;
      const rows = Math.ceil(h / gridSize) + 1;

      for (let r = 0; r < rows; r++) {
        for (let c = 0; c < cols; c++) {
          const x = c * gridSize;
          const y = r * gridSize;
          const dist = Math.sqrt((x - w * 0.5) ** 2 + (y - h * 0.45) ** 2);
          const wave = Math.sin(dist * 0.008 - time * 0.6) * 0.5 + 0.5;
          ctx.fillStyle = `rgba(245,158,11,${(0.03 + wave * 0.08) * am})`;
          ctx.beginPath();
          ctx.arc(x, y, 1, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      for (let r = 0; r < rows; r++) {
        const y = r * gridSize;
        const wave = Math.sin(y * 0.02 - time * 0.3) * 0.5 + 0.5;
        ctx.strokeStyle = `rgba(245,158,11,${(0.015 + wave * 0.02) * am})`;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }

      for (let c = 0; c < cols; c++) {
        const x = c * gridSize;
        const wave = Math.sin(x * 0.015 - time * 0.2) * 0.5 + 0.5;
        ctx.strokeStyle = `rgba(245,158,11,${(0.01 + wave * 0.015) * am})`;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();
      }

      const orbX = w * 0.5 + Math.sin(time * 0.3) * 60;
      const orbY = h * 0.45 + Math.cos(time * 0.2) * 40;
      const g1 = ctx.createRadialGradient(orbX, orbY, 0, orbX, orbY, 250);
      g1.addColorStop(0, `rgba(245,158,11,${0.06 * am})`);
      g1.addColorStop(0.5, `rgba(245,158,11,${0.02 * am})`);
      g1.addColorStop(1, "rgba(245,158,11,0)");
      ctx.fillStyle = g1;
      ctx.fillRect(0, 0, w, h);

      time += 0.016;
      animId = requestAnimationFrame(draw);
    };
    draw();

    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener("resize", resize);
    };
  }, []);

  // Look up the pairing code on mount (if provided in URL).
  useEffect(() => {
    if (!code) return;
    fetch(`/api/v1/auth/pairing-info?code=${encodeURIComponent(code)}`)
      .then((r) => r.json())
      .then((data: PairingInfo) => {
        if (data.valid) {
          setPairingInfo(data);
        } else {
          setError("This pairing code is invalid or has expired.");
        }
      })
      .catch(() => setError("Failed to verify pairing code."));
  }, [code]);

  const handleLookup = useCallback(async () => {
    if (!inputCode.trim()) return;
    setError("");
    setPairingInfo(null);

    try {
      const resp = await fetch(
        `/api/v1/auth/pairing-info?code=${encodeURIComponent(inputCode.trim())}`,
      );
      const data: PairingInfo = await resp.json();
      if (data.valid) {
        setPairingInfo(data);
      } else {
        setError("This pairing code is invalid or has expired.");
      }
    } catch {
      setError("Failed to verify pairing code.");
    }
  }, [inputCode]);

  const handleLink = useCallback(async () => {
    setStatus("loading");
    setError("");

    try {
      const resp = await authFetch("/api/v1/auth/link-channel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: inputCode.trim() }),
      });

      if (resp.ok) {
        setStatus("success");
      } else {
        const data = await resp.json().catch(() => null);
        setError(data?.detail ?? "Failed to link account.");
        setStatus("error");
      }
    } catch {
      setError("Network error. Please try again.");
      setStatus("error");
    }
  }, [inputCode]);

  const platformLabel = pairingInfo
    ? PLATFORM_LABELS[pairingInfo.platform] ?? pairingInfo.platform
    : "";

  return (
    <div className="font-mono bg-background text-foreground h-screen flex flex-col items-center justify-center overflow-hidden text-sm leading-normal antialiased relative">
      {/* animated grid */}
      <canvas
        ref={canvasRef}
        className="absolute inset-0 w-full h-full pointer-events-none"
      />

      {/* vignette */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            "radial-gradient(ellipse at 50% 45%, transparent 20%, var(--background) 70%)",
        }}
      />

      {/* scan line */}
      <div
        className="absolute left-0 right-0 h-px pointer-events-none animate-scan"
        style={{
          background:
            "linear-gradient(90deg, transparent, rgba(245,158,11,0.08), transparent)",
        }}
      />

      {/* theme toggle */}
      <Button
        variant="outline"
        size="icon"
        onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
        className="absolute top-6 right-6 z-20 bg-muted/80 backdrop-blur"
        aria-label="Toggle theme"
      >
        {resolvedTheme === "dark" ? (
          <SunIcon className="w-4 h-4" />
        ) : (
          <MoonIcon className="w-4 h-4" />
        )}
      </Button>

      {/* ── card ── */}
      <div
        className={cn(
          "relative z-10 w-full max-w-105 rounded-2xl border border-line bg-card/80 backdrop-blur-xl shadow-xl px-10 py-10 opacity-0",
          "animate-fade-up",
        )}
        style={{ animationDelay: "0.1s" }}
      >
        {/* hat watermark — top-right corner, tucked inside card */}
        <div
          className="absolute top-10 right-5 w-25 h-25 opacity-[0.5] pointer-events-none select-none"
          style={{
            backgroundColor: "var(--foreground)",
            maskImage: "url(/hat.svg)",
            WebkitMaskImage: "url(/hat.svg)",
            maskSize: "contain",
            WebkitMaskSize: "contain",
            maskRepeat: "no-repeat",
            WebkitMaskRepeat: "no-repeat",
            maskPosition: "center",
            WebkitMaskPosition: "center",
          }}
        />

        {/* brand */}
        <div className="flex items-center gap-3 mb-10">
          <img
            src="/login.svg"
            alt="Surogates"
            className="w-11 h-11 rounded-xl animate-logo-glow"
          />
          <div>
            <div className="font-extrabold text-2xl text-foreground tracking-tight">
              Surogates
            </div>
            <div className="text-xs text-muted-foreground tracking-[0.14em] uppercase">
              Managed Agents
            </div>
          </div>
        </div>

        {/* heading */}
        <div className="mb-8">
          <h3 className="text-2xl font-bold text-foreground tracking-tight">
            Link your account
          </h3>
          <p className="text-sm text-muted-foreground">
            connect your messaging platform
          </p>
        </div>

        {/* form */}
        {status === "success" ? (
          <div className="px-3.5 py-4 rounded-lg bg-emerald-500/10 border border-emerald-500/15 text-center space-y-2">
            <p className="text-sm font-medium text-emerald-700 dark:text-emerald-400">
              {platformLabel} account linked successfully!
            </p>
            <p className="text-sm text-muted-foreground">
              You can now send messages to the bot and it will recognize you.
            </p>
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (pairingInfo) {
                void handleLink();
              } else {
                void handleLookup();
              }
            }}
          >
            <div className="mb-10">
              <label
                className="block text-sm text-subtle font-medium mb-1.5 uppercase tracking-wide"
              >
                Pairing code
              </label>
              <div className="flex justify-center">
                <InputOTP
                  maxLength={8}
                  value={rawCode}
                  onChange={(value) => {
                    setRawCode(value.toUpperCase());
                    setError("");
                  }}
                  inputMode="text"
                  pattern="[A-Za-z0-9]*"
                >
                  <InputOTPGroup>
                    <InputOTPSlot index={0} />
                    <InputOTPSlot index={1} />
                    <InputOTPSlot index={2} />
                    <InputOTPSlot index={3} />
                  </InputOTPGroup>
                  <InputOTPSeparator />
                  <InputOTPGroup>
                    <InputOTPSlot index={4} />
                    <InputOTPSlot index={5} />
                    <InputOTPSlot index={6} />
                    <InputOTPSlot index={7} />
                  </InputOTPGroup>
                </InputOTP>
              </div>
            </div>

            {/* Platform info (shown after code lookup) */}
            {pairingInfo && (
              <div className="mb-4 px-3.5 py-2.5 rounded-lg bg-muted/30 border border-border text-sm text-center text-muted-foreground">
                Linking your <strong>{platformLabel}</strong> account
              </div>
            )}

            {error && (
              <div className="mb-4 px-3.5 py-2.5 rounded-lg bg-destructive/5 border border-destructive/15 text-sm text-destructive animate-[fade-in_0.2s_ease]">
                {error}
              </div>
            )}

            {pairingInfo ? (
              <Button
                type="submit"
                disabled={status === "loading"}
                className="w-full h-10 font-bold"
              >
                {status === "loading" ? (
                  <>
                    <Loader2Icon className="w-4 h-4 animate-spin" />
                    Linking...
                  </>
                ) : (
                  `Link ${platformLabel} account`
                )}
              </Button>
            ) : (
              <Button
                type="submit"
                variant="outline"
                disabled={rawCode.length < 8}
                className="w-full h-10 font-bold"
              >
                Verify code
              </Button>
            )}
          </form>
        )}
      </div>

      {/* tags strip below card */}
      <div
        className={cn("relative z-10 mt-8 opacity-0", "animate-fade-in")}
        style={{ animationDelay: "0.5s" }}
      >
        <div className="flex flex-wrap justify-center gap-2">
          {TAGS.map((tag) => (
            <div
              key={tag.label}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-card/50 border border-line/50 backdrop-blur-sm"
            >
              <span className="text-sm text-primary">{tag.icon}</span>
              <span className="text-sm text-faint">{tag.label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* footer */}
      <div className="absolute bottom-5 text-center text-[11px] text-faint z-10">
        Copyright &copy; 2026 Invergent SA &middot; All rights reserved.
      </div>
    </div>
  );
}
