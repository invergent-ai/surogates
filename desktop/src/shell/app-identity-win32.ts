// The app as Windows knows it. Its AppUserModelID, which Windows groups its windows' taskbar button by, names its
// toasts by and starts their clicks through: a desktop app raises a toast only with one that a shortcut in the Start
// menu carries too (Microsoft, "Enable desktop toast notifications through an AppUserModelID"; Electron,
// app.setAppUserModelId, set before the first window). Electron names the login item's Run key value by it too. With
// it, the CLSID of the app's toast activator, which the shortcut carries as well, so that a toast's click from the
// Action Center reaches the app (Microsoft, "Send a local toast notification from a C# app", the shortcut's
// System.AppUserModel.ToastActivatorCLSID; Electron, app.setToastActivatorCLSID, set before the first notification).
// The installed app's are APP_ID and APP_ACTIVATOR, which the MSI's shortcut carries; a development build's are its
// own, which test/win32/dev-app.ps1's shortcut carries, so that its login item and its toasts are never the installed
// app's.
//
// Its surogate:// links, taken per user, in the user's own classes (HKCU\Software\Classes\surogate; Microsoft,
// "Registering an Application to a URI Scheme"), by the command that starts it, at each start where Windows does not
// name that command already (Electron, app.setAsDefaultProtocolClient), each of its arguments quoted by Electron as
// the Run key's are (login-item-win32.ts; a test on the Windows runner measures both). Windows starts the command
// with the link as its last argument, "%1", after "--": a link is no switch of Chromium's, whatever it holds
// (base::CommandLine reads none after "--").
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

/** What of Electron's app takes its links where Windows keeps them. */
export interface LinkTaker {
  isDefaultProtocolClient(protocol: string, path?: string, args?: string[]): boolean;
  setAsDefaultProtocolClient(protocol: string, path?: string, args?: string[]): boolean;
}

/** *app* named to Windows: the installed app's AppUserModelID and toast activator where *packaged*, else a development build's. */
export function nameTheApp(app: NamedApp, packaged: boolean): void {
  app.setAppUserModelId(packaged ? APP_ID : DEVELOPMENT_ID);
  app.setToastActivatorCLSID(packaged ? APP_ACTIVATOR : DEVELOPMENT_ACTIVATOR);
}

/** surogate:// links, for this user, started by *command*, the app's own; said where Windows did not take them. */
export function takeLinks(app: LinkTaker, command: readonly string[]): void {
  const [path, ...rest] = command;
  const args = [...rest, "--"];
  if (app.isDefaultProtocolClient("surogate", path, args)) return;
  if (!app.setAsDefaultProtocolClient("surogate", path, args)) throw new Error("Windows did not give Surogate its surogate:// links");
}

/** Whether a launch with *argv* asks the app to quit: QUIT among its switches, before any "--". */
export function quitAsked(argv: readonly string[]): boolean {
  const end = argv.indexOf("--");
  return (end === -1 ? argv : argv.slice(0, end)).includes(QUIT);
}
