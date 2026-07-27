// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Password management for the profile tab. Firebase-backed users get a
// reset-email flow (their provider owns the credential); local database
// accounts get an in-app change-password form.

import { useCallback, useEffect, useState } from "react";
import { KeyRoundIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import {
  changePassword,
  fetchAuthConfig,
  type FirebaseRuntimeConfig,
} from "@/api/auth";
import {
  firebaseUserHasPasswordProvider,
  sendFirebasePasswordReset,
} from "@/features/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const MIN_PASSWORD_LENGTH = 8;

export function PasswordSection({
  authProvider,
  email,
}: {
  authProvider: string;
  email: string;
}) {
  if (authProvider.startsWith("firebase:")) {
    return <FirebaseResetPassword email={email} />;
  }
  if (authProvider === "database") {
    return <DatabaseChangePassword />;
  }
  return null;
}

function SectionShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="border-t border-line pt-5 mt-6">
      <div className="flex items-center gap-2 mb-3">
        <KeyRoundIcon className="w-4 h-4 text-muted-foreground" />
        <h2 className="text-sm font-semibold text-foreground">Password</h2>
      </div>
      {children}
    </div>
  );
}

function FirebaseResetPassword({ email }: { email: string }) {
  const [sending, setSending] = useState(false);
  // ``undefined`` = still resolving; ``null`` = no password credential
  // (Google/GitHub-only — hide entirely); otherwise the config to reset
  // against.
  const [config, setConfig] = useState<
    FirebaseRuntimeConfig | null | undefined
  >(undefined);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const authConfig = await fetchAuthConfig();
      if (cancelled) return;
      if (!authConfig.firebase) {
        setConfig(null);
        return;
      }
      const hasPassword = await firebaseUserHasPasswordProvider(
        authConfig.firebase,
      );
      if (!cancelled) setConfig(hasPassword ? authConfig.firebase : null);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleReset = useCallback(async () => {
    if (!config) return;
    setSending(true);
    try {
      await sendFirebasePasswordReset(config, email);
    } catch {
      // Fall through to the neutral notice — never leak whether the
      // address is registered.
    } finally {
      setSending(false);
      toast.success("Password-reset link sent to your email.");
    }
  }, [config, email]);

  // Only email/password accounts have a password to reset. While the
  // provider check is in flight, or for Google/GitHub-only accounts,
  // render nothing.
  if (!config) return null;

  return (
    <SectionShell>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="outline"
          onClick={() => void handleReset()}
          disabled={sending}
          className="gap-2"
        >
          {sending ? (
            <Loader2Icon className="w-4 h-4 animate-spin" />
          ) : (
            <KeyRoundIcon className="w-4 h-4" />
          )}
          Send reset email
        </Button>
        <span className="text-sm text-muted-foreground">
          We will email a link to {email} to set a new password.
        </span>
      </div>
    </SectionShell>
  );
}

function DatabaseChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);

  const tooShort = next.length > 0 && next.length < MIN_PASSWORD_LENGTH;
  const mismatch = confirm.length > 0 && next !== confirm;
  const canSubmit =
    current.length > 0 &&
    next.length >= MIN_PASSWORD_LENGTH &&
    next === confirm &&
    !saving;

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setSaving(true);
    try {
      await changePassword({ current_password: current, new_password: next });
      setCurrent("");
      setNext("");
      setConfirm("");
      toast.success("Password updated.");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [canSubmit, current, next]);

  const error = tooShort
    ? `Use at least ${MIN_PASSWORD_LENGTH} characters.`
    : mismatch
      ? "Passwords do not match."
      : null;

  return (
    <SectionShell>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          void handleSubmit();
        }}
      >
        <Input
          type="password"
          autoComplete="current-password"
          aria-label="Current password"
          placeholder="Current password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <Input
            type="password"
            autoComplete="new-password"
            aria-label="New password"
            placeholder="New password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
          <Input
            type="password"
            autoComplete="new-password"
            aria-label="Confirm new password"
            placeholder="Confirm password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </div>
        <div className="flex items-center gap-3">
          <Button type="submit" disabled={!canSubmit} className="gap-2">
            {saving ? (
              <Loader2Icon className="w-4 h-4 animate-spin" />
            ) : (
              <KeyRoundIcon className="w-4 h-4" />
            )}
            Update password
          </Button>
          {error && <span className="text-sm text-destructive">{error}</span>}
        </div>
      </form>
    </SectionShell>
  );
}
