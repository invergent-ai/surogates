// The app as this OS knows it, where the app tells the OS itself (app-identity-win32.ts): the id its windows, its
// notifications and its login item go by, and a launch that asks it to quit. Linux's install script names it in its
// desktop entry (install.sh), and macOS reads it from the app's bundle (Info.plist): on either the app tells nothing,
// and no launch asks it to quit. No OS's app registers its surogate:// links: its install does (deep-link.ts).
// Electron-free.

import { forThisOs, notBuilt } from "../platform.js";
import * as win32 from "./app-identity-win32.js";

interface AppIdentity {
  nameTheApp(app: win32.NamedApp, packaged: boolean): void;
  quitAsked(argv: readonly string[]): boolean;
}
const told: AppIdentity = { nameTheApp: () => {}, quitAsked: () => false };
const IDENTITY: AppIdentity = forThisOs<AppIdentity>({ linux: told, darwin: told, win32 }) ?? {
  nameTheApp: () => notBuilt("Surogate Desktop's name for the system"),
  quitAsked: () => false,
};

/** *app* named to this OS, before its first window, *packaged* or a development build. */
export const nameTheApp = (app: win32.NamedApp, packaged: boolean): void => IDENTITY.nameTheApp(app, packaged);

/** Whether a launch with *argv*, the app's own or a second one's, asks the app to quit. */
export const quitAsked = (argv: readonly string[]): boolean => IDENTITY.quitAsked(argv);
