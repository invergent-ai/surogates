// The Start menu shortcut of a development build (dev-app.ps1): Electron's shell.writeShortcutLink, which sets the
// shortcut's AppUserModelID and toast activator as the app sets its own (src/shell/app-identity-win32.ts), as no
// PowerShell cmdlet can. Run by this package's Electron: electron.exe dev-shortcut.cjs <shortcut> <main> <id> <clsid>.
// It exits 0 once the shortcut is made. Its own Electron data is kept in one folder of the user's temporary folder.

const { tmpdir } = require("node:os");
const { join } = require("node:path");

const { app, shell } = require("electron");

const [shortcut, main, id, clsid] = process.argv.slice(-4);
app.setPath("userData", join(tmpdir(), "surogate-dev-shortcut"));
void app.whenReady().then(() => {
  const made = shell.writeShortcutLink(shortcut, {
    target: process.execPath, args: `"${main}"`, appUserModelId: id, toastActivatorClsid: clsid, description: "Surogate, a development build",
  });
  app.exit(made ? 0 : 1);
});
