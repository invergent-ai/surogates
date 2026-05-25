// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect, useRef, useState } from "react";
import { useTheme } from "next-themes";
import { useNavigate } from "@tanstack/react-router";
import { SunIcon, MoonIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import {
  exchangeFirebaseToken,
  fetchAuthConfig,
  type AuthConfigResponse,
} from "@/api/auth";
import {
  createFirebaseEmailAccount,
  friendlyAuthError,
  signInWithFirebaseEmail,
  signInWithGithub,
  signInWithGoogle,
} from "./firebase";
import { storeAuthTokens, getPostAuthRoute } from "./session";

const TAGS = [
  { label: "Managed Agents", icon: "⬡" },
  { label: "MCP Integration", icon: "⚡" },
  { label: "Tool Governance", icon: "◈" },
  { label: "Multi-Tenant", icon: "◇" },
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
  // Informational notice (e.g. "Check your inbox to verify your email")
  // — separate from loginError so the user sees a neutral message
  // instead of a red destructive alert during the happy path.
  const [loginNotice, setLoginNotice] = useState<string | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfigResponse>({
    self_registration_enabled: false,
    firebase: null,
  });
  const [firebaseMode, setFirebaseMode] = useState<"sign-in" | "create">(
    "sign-in",
  );

  /* ── load runtime auth config once on mount ── */
  useEffect(() => {
    let cancelled = false;
    fetchAuthConfig()
      .then((config) => {
        if (!cancelled) setAuthConfig(config);
      })
      .catch(() => {
        if (!cancelled) {
          setAuthConfig({ self_registration_enabled: false, firebase: null });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const finishFirebaseUser = async (user: {
    getIdToken: (forceRefresh?: boolean) => Promise<string>;
  }) => {
    // Force-refresh the ID token so claims reflect the current
    // server-side state (notably ``email_verified`` after the user
    // returns from clicking the verification link).
    const idToken = await user.getIdToken(true);
    const tokens = await exchangeFirebaseToken(idToken);
    storeAuthTokens(tokens.access_token, tokens.refresh_token);
    void navigate({ to: getPostAuthRoute() });
  };

  /** Exchange the token only when the user's email is verified.
   *
   * Firebase email/password sign-up always returns ``emailVerified=false``
   * until the user clicks the link in the verification mail Firebase
   * sends (we trigger it in ``createFirebaseEmailAccount``). Gating the
   * exchange here means a brand-new account can't slip into a Surogates
   * session without proving inbox ownership — matching the ops shell. */
  const finishIfVerified = async (user: {
    emailVerified: boolean;
    getIdToken: (forceRefresh?: boolean) => Promise<string>;
  }) => {
    if (!user.emailVerified) {
      setLoginNotice(
        "Check your inbox and click the verification link, then sign in again.",
      );
      return;
    }
    await finishFirebaseUser(user);
  };

  const runFirebaseAction = async (
    name: string,
    action: () => Promise<{
      emailVerified: boolean;
      getIdToken: (forceRefresh?: boolean) => Promise<string>;
    }>,
  ) => {
    setLoginError(null);
    setLoginNotice(null);
    setIsLoading(true);
    try {
      // Google / GitHub providers already return verified emails so
      // ``finishIfVerified`` is a no-op gate for them; routing through
      // it keeps a single sign-in path and protects against future
      // providers that may return ``emailVerified=false``.
      await finishIfVerified(await action());
    } catch (err) {
      setLoginError(friendlyAuthError(err, `${name} sign-in failed.`));
    } finally {
      setIsLoading(false);
    }
  };

  const showFirebase =
    authConfig.self_registration_enabled && authConfig.firebase !== null;
  const firebaseProviders = authConfig.firebase?.enabled_providers ?? [];

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

  const firebasePasswordEnabled =
    showFirebase &&
    authConfig.firebase !== null &&
    firebaseProviders.includes("password");

  const handleLogin = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!email || !password) {
      setLoginError("Please enter both email and password");
      return;
    }
    setLoginError(null);
    setLoginNotice(null);
    setIsLoading(true);

    // ── Create-account mode: sign up via Firebase email/password. ──
    const firebaseConfig = authConfig.firebase;
    if (firebaseMode === "create" && firebasePasswordEnabled && firebaseConfig) {
      try {
        await finishIfVerified(
          await createFirebaseEmailAccount(firebaseConfig, email, password),
        );
      } catch (err) {
        setLoginError(friendlyAuthError(err, "Sign-up failed."));
      } finally {
        setIsLoading(false);
      }
      return;
    }

    // ── Sign-in mode: try local DB auth first so manually-created
    //    users keep working, then fall back to Firebase email sign-in
    //    when the password provider is enabled for this agent. ──
    try {
      const response = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      if (response.ok) {
        const payload = (await response.json()) as {
          access_token: string;
          refresh_token: string;
          token_type: string;
        };
        storeAuthTokens(payload.access_token, payload.refresh_token);
        void navigate({ to: getPostAuthRoute() });
        return;
      }

      if (response.status === 401 && firebasePasswordEnabled && firebaseConfig) {
        // Local credentials don't match — try Firebase email next.
        try {
          await finishIfVerified(
            await signInWithFirebaseEmail(firebaseConfig, email, password),
          );
          return;
        } catch (firebaseErr) {
          setLoginError(
            friendlyAuthError(firebaseErr, "Incorrect email or password."),
          );
          return;
        }
      }

      // Backend rejected the login. Conflate detail messages into a
      // single neutral string so the form doesn't leak whether the
      // email is registered.
      setLoginError("Incorrect email or password.");
    } catch (err) {
      setLoginError(
        friendlyAuthError(err, "Sign-in failed. Please try again."),
      );
    } finally {
      setIsLoading(false);
    }
  };

  const clearError = () => {
    setLoginError(null);
    setLoginNotice(null);
  };

  return (
    <div className="bg-background text-foreground min-h-dvh flex flex-col items-center justify-center overflow-hidden text-sm leading-normal antialiased relative px-4 py-6">
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
        type="button"
        variant="outline"
        size="icon-sm"
        onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
        className="absolute top-6 right-6 z-20 size-9 rounded-lg border-border bg-muted/80 text-muted-foreground normal-case backdrop-blur hover:border-primary/30 hover:text-foreground"
        aria-label="Toggle theme"
      >
        {resolvedTheme === "dark" ? (
          <SunIcon className="w-4 h-4" />
        ) : (
          <MoonIcon className="w-4 h-4" />
        )}
      </Button>

      {/* ── card ── */}
      <Card
        className={cn(
          "relative z-10 block w-full max-w-[420px] overflow-visible rounded-2xl border border-line bg-card/80 px-6 py-8 sm:px-10 sm:py-10 text-card-foreground opacity-0 shadow-xl backdrop-blur-xl ring-0",
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
            <div className="text-xs text-muted-foreground tracking-[0.14em] uppercase">
              Managed Agent
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
            <Label htmlFor="login-email" className="mb-1.5 block text-subtle">
              Email
            </Label>
            <Input
              id="login-email"
              type="email"
              value={email}
              onChange={(e) => { setEmail(e.target.value); clearError(); }}
              placeholder="you@company.com"
              aria-invalid={!!loginError && !email}
            />
          </div>

          <div className="mb-5">
            <Label htmlFor="login-password" className="mb-1.5 block text-subtle">
              Password
            </Label>
            <div className="relative">
              <Input
                id="login-password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => { setPassword(e.target.value); clearError(); }}
                placeholder="••••••••••••"
                aria-invalid={!!loginError && !password}
              />
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-2 top-1/2 h-7 -translate-y-1/2 rounded-md px-2 text-xs font-medium tracking-normal text-faint normal-case hover:bg-transparent hover:text-foreground"
              >
                {showPassword ? "Hide" : "Show"}
              </Button>
            </div>
          </div>

          {loginError && (
            <Alert
              variant="destructive"
              className="mb-4 rounded-lg border-destructive/15 bg-destructive/5 px-3.5 py-2.5 text-sm animate-[fade-in_0.2s_ease] after:hidden"
            >
              <AlertDescription className="text-sm text-destructive">
                {loginError}
              </AlertDescription>
            </Alert>
          )}

          {loginNotice && (
            <Alert className="mb-4 rounded-lg border-primary/20 bg-primary/5 px-3.5 py-2.5 text-sm animate-[fade-in_0.2s_ease] after:hidden">
              <AlertDescription className="text-sm text-foreground">
                {loginNotice}
              </AlertDescription>
            </Alert>
          )}

          <Button
            type="submit"
            disabled={isLoading}
            size="lg"
          >
            {isLoading ? (
              <>
                <Spinner className="size-4 text-primary-foreground" />
                {firebaseMode === "create"
                  ? "Creating account..."
                  : "Signing in..."}
              </>
            ) : firebaseMode === "create" ? (
              "Create account"
            ) : (
              "Sign in"
            )}
          </Button>

          {firebasePasswordEnabled && (
            <button
              type="button"
              className="mt-3 block text-center text-[11px] text-faint hover:text-foreground transition-colors"
              onClick={() => {
                clearError();
                setFirebaseMode((m) => (m === "create" ? "sign-in" : "create"));
              }}
            >
              {firebaseMode === "create"
                ? "Already have an account? Sign in"
                : "New here? Create an account"}
            </button>
          )}
        </form>

        {/* ── Firebase self-registration ── */}
        {showFirebase && authConfig.firebase && (() => {
          // Capture the narrowed reference so the button onClicks don't
          // need to re-check ``authConfig.firebase`` (and don't reach for
          // a non-null assertion that the lint rule forbids).
          const firebase = authConfig.firebase;
          return (
            <div className="mt-6 border-t border-line pt-5">
              <p className="mb-3 text-[11px] uppercase tracking-widest text-faint">
                Or continue with
              </p>

              {firebaseProviders.includes("google") && (
                <Button
                  type="button"
                  variant="outline"
                  size="lg"
                  className="mb-2 w-full"
                  disabled={isLoading}
                  onClick={() =>
                    runFirebaseAction(
                      "Google",
                      () => signInWithGoogle(firebase),
                    )
                  }
                >
                  Google
                </Button>
              )}

              {firebaseProviders.includes("github") && (
                <Button
                  type="button"
                  variant="outline"
                  size="lg"
                  className="mb-2 w-full"
                  disabled={isLoading}
                  onClick={() =>
                    runFirebaseAction(
                      "GitHub",
                      () => signInWithGithub(firebase),
                    )
                  }
                >
                  GitHub
                </Button>
              )}

            </div>
          );
        })()}
      </Card>

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
