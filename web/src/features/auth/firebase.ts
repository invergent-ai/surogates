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
  signInWithEmailAndPassword,
  signInWithPopup,
  type Auth,
  type User,
} from "firebase/auth";

import type { FirebaseRuntimeConfig } from "@/api/auth";

let app: FirebaseApp | null = null;
let auth: Auth | null = null;
let activeProjectId: string | null = null;

export function getRuntimeFirebaseAuth(config: FirebaseRuntimeConfig): Auth {
  if (!app || activeProjectId !== config.project_id) {
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
  return auth!;
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
): Promise<User> {
  return (
    await createUserWithEmailAndPassword(
      getRuntimeFirebaseAuth(config),
      email.trim(),
      password,
    )
  ).user;
}
