// The app as Windows knows it. Its AppUserModelID, which Windows groups its windows' taskbar button by, names its
// toasts by and starts their clicks through: a desktop app raises a toast only with one that a shortcut in the Start
// menu carries too (Microsoft, "Enable desktop toast notifications through an AppUserModelID"; Electron,
// app.setAppUserModelId, set before the first window). Electron names the login item's Run key value by it too. With
// it, the CLSID of the app's toast activator, which the shortcut carries as well, so that a toast's click from the
// Action Center reaches the app (Microsoft, "Send a local toast notification from a C# app", the shortcut's
// System.AppUserModel.ToastActivatorCLSID; Electron, app.setToastActivatorCLSID, set before the first notification).
// The installed app's are APP_ID and APP_ACTIVATOR, which the MSI's shortcut carries; a development build's are its
// own, which test/win32/dev-app.ps1's shortcut carries, so that its login item and its toasts are never the installed
// app's. The app registers no surogate:// scheme: the MSI does, machine-wide, and dev-app.ps1 for a development build's
// user, as Linux's install script does (deep-link.ts).
//
// And a launch with QUIT among its arguments, which asks the running app to quit as its menu's Quit does, as a script
// that removes a development build asks it (dev-app.ps1); where none runs, it quits at once.

export const APP_ID = "ai.invergent.surogate";
export const APP_ACTIVATOR = "{6E271D8E-0539-4AF2-8B3D-57150B0C7113}";
export const DEVELOPMENT_ID = "ai.invergent.surogate.development";
export const DEVELOPMENT_ACTIVATOR = "{73E63950-1345-41A7-A15E-AE7A7F851999}";
export const QUIT = "--quit";

/** What of Electron's app names the app to Windows. */
export interface NamedApp {
  setAppUserModelId(id: string): void;
  setToastActivatorCLSID(id: string): void;
}

/** *app* named to Windows: the installed app's AppUserModelID and toast activator where *packaged*, else a development build's. */
export function nameTheApp(app: NamedApp, packaged: boolean): void {
  app.setAppUserModelId(packaged ? APP_ID : DEVELOPMENT_ID);
  app.setToastActivatorCLSID(packaged ? APP_ACTIVATOR : DEVELOPMENT_ACTIVATOR);
}

/** Whether a launch with *argv* asks the app to quit: QUIT among its switches, before any "--". */
export function quitAsked(argv: readonly string[]): boolean {
  const end = argv.indexOf("--");
  return (end === -1 ? argv : argv.slice(0, end)).includes(QUIT);
}
