// About Surogate's page: the app's name and version, which a click copies with what the app
// runs on, and its documentation. Escape or Close closes it. All text comes from the main
// process and is set with textContent only.

import { byId, mark, markTheme } from "./ui.js";

interface About {
  state(): Promise<{ version: string }>;
  copy(): Promise<void>;
  documentation(): Promise<void>;
  close(): Promise<void>;
}

const about = (globalThis as unknown as { surogateAbout: About }).surogateAbout;

markTheme();
byId("about-mark").append(mark(64));

let said: ReturnType<typeof setTimeout> | undefined;
byId("version").addEventListener("click", () => void about.copy().then(() => {
  byId("copied").textContent = "Copied to the clipboard";
  clearTimeout(said);
  said = setTimeout(() => {
    byId("copied").textContent = "";
  }, 2000);
}));
byId("documentation").addEventListener("click", () => void about.documentation());
byId("close").addEventListener("click", () => void about.close());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") void about.close();
});
void about.state().then(({ version }) => {
  byId("version").textContent = `Version ${version}`;
  byId("version").setAttribute("aria-label", `Copy version ${version} to the clipboard`);
  byId("close").focus();
});
