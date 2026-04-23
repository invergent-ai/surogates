// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect, useRef, useState } from "react";
import { useTheme } from "next-themes";
import { useNavigate } from "@tanstack/react-router";
import { SunIcon, MoonIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { storeAuthTokens, getPostAuthRoute } from "./session";

const TAGS = [
  { label: "Managed Agents", icon: "⬡" },
  { label: "MCP Integration", icon: "⚡" },
  { label: "Tool Governance", icon: "◈" },
  { label: "Multi-Tenant", icon: "◇" },
  { label: "K8s Native", icon: "☁" },
] as const;

export function LoginPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { resolvedTheme, setTheme } = useTheme();
  const navigate = useNavigate();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [focusedField, setFocusedField] = useState<string | null>(null);

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

  const handleLogin = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!email || !password) {
      setLoginError("Please enter both email and password");
      return;
    }
    setLoginError(null);
    setIsLoading(true);

    try {
      const response = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as {
          detail?: string;
        } | null;
        throw new Error(payload?.detail ?? "Invalid credentials.");
      }

      const payload = (await response.json()) as {
        access_token: string;
        refresh_token: string;
        token_type: string;
      };
      storeAuthTokens(payload.access_token, payload.refresh_token);
      void navigate({ to: getPostAuthRoute() });
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : "Auth failed.");
    } finally {
      setIsLoading(false);
    }
  };

  const clearError = () => setLoginError(null);

  const inputCls = (field: string, hasError = false) =>
    cn(
      "w-full py-[11px] px-3.5 rounded-lg border  text-sm text-foreground bg-input outline-none transition-all duration-200",
      focusedField === field
        ? "border-primary/25 shadow-[0_0_0_3px_rgba(245,158,11,0.06)]"
        : hasError
          ? "border-destructive/20"
          : "border-border",
    );

  return (
    <div className=" bg-background text-foreground h-screen flex flex-col items-center justify-center overflow-hidden text-sm leading-normal antialiased relative">
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
      <button
        type="button"
        onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
        className="absolute top-6 right-6 w-9 h-9 rounded-lg border border-border bg-muted/80 backdrop-blur flex items-center justify-center cursor-pointer text-muted-foreground transition-colors duration-150 hover:text-foreground hover:border-primary/30 z-20"
        aria-label="Toggle theme"
      >
        {resolvedTheme === "dark" ? (
          <SunIcon className="w-4 h-4" />
        ) : (
          <MoonIcon className="w-4 h-4" />
        )}
      </button>

      {/* ── card ── */}
      <div
        className={cn(
          "relative z-10 w-full max-w-[420px] rounded-2xl border border-line bg-card/80 backdrop-blur-xl shadow-xl px-10 py-10 opacity-0",
          "animate-fade-up",
        )}
        style={{ animationDelay: "0.1s" }}
      >
        {/* hat watermark — top-right corner, tucked inside card */}
        <div
          className="absolute top-10 right-5 w-[100px] h-[100px] opacity-[0.5] pointer-events-none select-none"
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
        <div className="flex items-center gap-3 mb-8">
          <img
            src="/login.svg"
            alt="Surogates"
            className="w-11 h-11 rounded-xl animate-logo-glow"
          />
          <div>
            <div className="font-extrabold text-lg text-foreground tracking-tight">
              Surogates
            </div>
            <div className="text-[10px] text-muted-foreground tracking-[0.14em] uppercase">
              Managed Agent Platform
            </div>
          </div>
        </div>

        {/* heading */}
        <div className="mb-6">
          <h3 className="text-2xl font-bold text-foreground tracking-tight">
            Sign in
          </h3>
          <p className="text-sm text-muted-foreground">
            to access your agent
          </p>
        </div>

        {/* form */}
        <form onSubmit={handleLogin}>
          <div className="mb-3.5">
            <label htmlFor="login-email" className="block text-xs text-subtle font-medium mb-1.5 uppercase tracking-wide">
              Email
            </label>
            <input
              id="login-email"
              type="email"
              value={email}
              onChange={(e) => { setEmail(e.target.value); clearError(); }}
              onFocus={() => setFocusedField("email")}
              onBlur={() => setFocusedField(null)}
              placeholder="you@company.com"
              className={inputCls("email", !!loginError && !email)}
            />
          </div>

          <div className="mb-5">
            <label htmlFor="login-password" className="block text-xs text-subtle font-medium mb-1.5 uppercase tracking-wide">
              Password
            </label>
            <div className="relative">
              <input
                id="login-password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => { setPassword(e.target.value); clearError(); }}
                onFocus={() => setFocusedField("password")}
                onBlur={() => setFocusedField(null)}
                placeholder="••••••••••••"
                className={cn(inputCls("password"), "pr-11")}
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 bg-transparent border-none text-faint cursor-pointer text-xs font-medium"
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
          </div>

          {loginError && (
            <div className="mb-4 px-3.5 py-2.5 rounded-lg bg-destructive/5 border border-destructive/15 text-sm text-destructive animate-[fade-in_0.2s_ease]">
              {loginError}
            </div>
          )}

          <button
            type="submit"
            disabled={isLoading}
            className={cn(
              "w-full py-3 rounded-lg border-none text-sm font-bold tracking-tight flex items-center justify-center gap-2 transition-all duration-200",
              isLoading
                ? "bg-amber-600/50 cursor-not-allowed text-primary-foreground"
                : "bg-gradient-to-br from-amber-500 to-amber-600 cursor-pointer text-primary-foreground shadow-[0_4px_24px_rgba(245,158,11,0.15)] hover:shadow-[0_4px_32px_rgba(245,158,11,0.25)]",
            )}
          >
            {isLoading ? (
              <>
                <span className="w-4 h-4 border-2 border-primary-foreground/25 border-t-primary-foreground rounded-full animate-spinner" />
                Signing in...
              </>
            ) : (
              "Sign in"
            )}
          </button>
        </form>
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
              <span className="text-xs text-primary">{tag.icon}</span>
              <span className="text-xs text-faint">{tag.label}</span>
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
