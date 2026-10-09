// Quick entry's page: a box whose text starts a new chat. Enter sends it, Shift+Enter or Alt+Enter
// is a new line, and a key that ends or cancels a composition is the composition's. Escape, or a
// click beside the box, hides the window. The text stays until the new chat's page says it went.
// What the main process says is set with textContent only.

import { byId, markTheme } from "./ui.js";

interface Quick {
  send(text: string): Promise<string | null>; // why it was not sent, or null once it was
  dismiss(): Promise<void>;
}

const quick = (globalThis as unknown as { surogateQuick: Quick }).surogateQuick;

markTheme();

const text = byId<HTMLTextAreaElement>("text");
// Each send's number: only the last one's answer is said, as a newer message takes the place of one still on its way.
let sends = 0;

document.addEventListener("keydown", (event) => {
  // An input method's Enter or Escape ends or cancels its composition.
  if (event.isComposing) return;
  if (event.key === "Escape") {
    void quick.dismiss();
    return;
  }
  if (event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  // Chromium's text box puts nothing in for Alt+Enter: the line break is put in here.
  if (event.altKey) {
    text.setRangeText("\n", text.selectionStart, text.selectionEnd, "end");
    return;
  }
  const sent = text.value;
  const sending = ++sends;
  byId("refused").textContent = "";
  void quick.send(sent).then((refused) => {
    if (sending !== sends) return;
    byId("refused").textContent = refused ?? "";
    // What was typed meanwhile stays.
    if (refused === null && text.value === sent) text.value = "";
  });
});
document.body.addEventListener("click", (event) => {
  if (!byId("box").contains(event.target as Node)) void quick.dismiss();
});
// Shown again, the keyboard is the box's.
addEventListener("focus", () => text.focus());
text.focus();
