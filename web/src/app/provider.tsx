// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Toaster } from "@/components/ui/sonner";
import { getDesktop } from "@/lib/desktop-bridge";
import { ThemeProvider } from "next-themes";
import { type ReactNode, useSyncExternalStore } from "react";

interface AppProviderProps {
  children: ReactNode;
}

const DARK = "(prefers-color-scheme: dark)";

function hearSystemTheme(changed: () => void): () => void {
  const query = window.matchMedia(DARK);
  query.addEventListener("change", changed);
  return () => query.removeEventListener("change", changed);
}

const systemTheme = () => (window.matchMedia(DARK).matches ? "dark" : "light");

/**
 * The theme Surogate Desktop gives the page, or none in a browser. The desktop's Light, Dark or
 * Match system drives prefers-color-scheme in every page it shows (Electron's
 * nativeTheme.themeSource), so the page follows that, live, and no toggle of its own applies.
 */
function useDesktopTheme(): string | undefined {
  const theme = useSyncExternalStore(hearSystemTheme, systemTheme);
  return getDesktop() ? theme : undefined;
}

export function AppProvider({ children }: AppProviderProps) {
  const forcedTheme = useDesktopTheme();
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="light"
      forcedTheme={forcedTheme}
    >
      {children}
      <Toaster position="bottom-right" visibleToasts={2} expand={true} />
    </ThemeProvider>
  );
}
