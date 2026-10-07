// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { getDesktop } from "@/lib/desktop-bridge";
import { requireGuest } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const LoginPage = lazy(() =>
  import("@/features/auth").then((m) => ({ default: m.LoginPage })),
);
const DesktopSignIn = lazy(() =>
  import("@/features/auth/desktop-sign-in").then((m) => ({ default: m.DesktopSignIn })),
);

// In Surogate Desktop's window the app signs in itself: the page takes its session from the app.
function SignIn() {
  return getDesktop() ? <DesktopSignIn /> : <LoginPage />;
}

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  beforeLoad: () => requireGuest(),
  component: SignIn,
});
