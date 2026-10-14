// The main window's controls on Windows: Linux's (main-window-linux.ts), Windows's own minimise, maximise and close
// over the frameless window's top right corner, in the shell's colours (Electron, titleBarStyle "hidden" with
// titleBarOverlay, Windows and Linux alike), painted again at each change of theme. The pages leave them their room
// by the window's own title bar area (shell.css, env(titlebar-area-x) and env(titlebar-area-width)), as on Linux. And
// the menu each of the app's other windows drops: Windows gives every window the app's menu, as Linux does.

export { windowControls } from "./main-window-linux.js";
