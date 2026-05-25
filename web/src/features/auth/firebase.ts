// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Runtime Firebase initialization for BYO self-registration. The
// Firebase project + API key are not known at build time — they are
// resolved per-deployment from `/api/v1/auth/config`. The web app
// initializes a Firebase App lazily on first sign-in and caches it
// keyed by project id so toggling between two BYO projects within one
// page lifetime (rare, but legal) doesn't reuse the wrong project's
// Auth instance.

import { initializeApp, type FirebaseApp } from "firebase/app";
import {
  GithubAuthProvider,
  GoogleAuthProvider,
  createUserWithEmailAndPassword,
  getAuth,
  sendEmailVerification,
  signInWithEmailAndPassword,
  signInWithPopup,
  type Auth,
  type User,
} from "firebase/auth";

import type { FirebaseRuntimeConfig } from "@/api/auth";

// ── Friendly error messages ─────────────────────────────────────────
//
// Firebase surfaces errors like ``Firebase: Error (auth/invalid-credential).``
// which are useless (and slightly intimidating) to end users. We map the
// codes we expect to surface to neutral, action-oriented strings and
// deliberately conflate "user not found / wrong password / bad
// credential" into a single message so the form doesn't leak whether
// an email is registered.
const FRIENDLY_AUTH_ERRORS: Record<string, string> = {
  "auth/invalid-credential": "Incorrect email or password.",
  "auth/invalid-email": "Please enter a valid email address.",
  "auth/wrong-password": "Incorrect email or password.",
  "auth/user-not-found": "Incorrect email or password.",
  "auth/user-disabled": "This account has been disabled.",
  "auth/email-already-in-use":
    "An account with this email already exists. Try signing in instead.",
  "auth/weak-password": "Password must be at least 6 characters.",
  "auth/too-many-requests":
    "Too many attempts. Please wait a moment and try again.",
  "auth/network-request-failed":
    "Network error. Please check your connection and try again.",
  "auth/popup-closed-by-user": "Sign-in was cancelled.",
  "auth/cancelled-popup-request": "Sign-in was cancelled.",
  "auth/popup-blocked":
    "Sign-in popup was blocked. Please allow popups for this site and try again.",
  "auth/account-exists-with-different-credential":
    "An account with this email already exists using a different sign-in method.",
  "auth/operation-not-allowed":
    "This sign-in method is not enabled. Please contact the administrator.",
  "auth/requires-recent-login":
    "Please sign in again to perform this action.",
};

/** Translate a thrown error into a user-safe string.
 *
 * Recognised Firebase error codes are mapped to the table above; other
 * errors fall back to the provided ``fallback`` so we never surface
 * internal SDK text like ``Firebase: Error (auth/...).`` to end users.
 */
export function friendlyAuthError(err: unknown, fallback: string): string {
  const code =
    typeof err === "object" && err !== null && "code" in err
      ? String((err as { code: unknown }).code)
      : "";
  if (code && FRIENDLY_AUTH_ERRORS[code]) {
    return FRIENDLY_AUTH_ERRORS[code];
  }
  return fallback;
}

let app: FirebaseApp | null = null;
let auth: Auth | null = null;
let activeProjectId: string | null = null;

export function getRuntimeFirebaseAuth(config: FirebaseRuntimeConfig): Auth {
  if (!app || !auth || activeProjectId !== config.project_id) {
    app = initializeApp(
      {
        apiKey: config.api_key,
        authDomain: config.auth_domain,
        projectId: config.project_id,
        appId: config.app_id ?? undefined,
        messagingSenderId: config.messaging_sender_id ?? undefined,
        measurementId: config.measurement_id ?? undefined,
      },
      // Name the app instance with the project id so two BYO projects
      // can coexist without clashing on the Firebase JS SDK's internal
      // default-app registry.
      `agent-${config.project_id}`,
    );
    auth = getAuth(app);
    activeProjectId = config.project_id;
  }
  return auth;
}

export async function signInWithGoogle(
  config: FirebaseRuntimeConfig,
): Promise<User> {
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  return (await signInWithPopup(getRuntimeFirebaseAuth(config), provider)).user;
}

export async function signInWithGithub(
  config: FirebaseRuntimeConfig,
): Promise<User> {
  const provider = new GithubAuthProvider();
  provider.addScope("user:email");
  return (await signInWithPopup(getRuntimeFirebaseAuth(config), provider)).user;
}

export async function signInWithFirebaseEmail(
  config: FirebaseRuntimeConfig,
  email: string,
  password: string,
): Promise<User> {
  return (
    await signInWithEmailAndPassword(
      getRuntimeFirebaseAuth(config),
      email.trim(),
      password,
    )
  ).user;
}

export async function createFirebaseEmailAccount(
  config: FirebaseRuntimeConfig,
  email: string,
  password: string,
): Promise<{ user: User; verificationSent: boolean; verificationError?: unknown }> {
  const { user } = await createUserWithEmailAndPassword(
    getRuntimeFirebaseAuth(config),
    email.trim(),
    password,
  );
  // Firebase doesn't auto-send verification emails — trigger it now so
  // the user receives one. If this fails we still return the (now-
  // existing) Firebase user; callers surface the error and offer a
  // resend button so the user isn't stuck with no path forward.
  try {
    await sendEmailVerification(user);
    return { user, verificationSent: true };
  } catch (err) {
    return { user, verificationSent: false, verificationError: err };
  }
}

/** Re-send the Firebase verification email for an already-signed-in user. */
export async function resendFirebaseEmailVerification(
  user: User,
): Promise<void> {
  await sendEmailVerification(user);
}
