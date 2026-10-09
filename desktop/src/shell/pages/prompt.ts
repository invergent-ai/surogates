// One of the desktop's prompts (spec, Section 4), drawn from what the main process
// sends. Every string is set as text, with each special character in it shown as its
// code point. The buttons answer the main process, which takes no answer before the
// input protection has passed, nor one from a key or a press that came sooner than
// that after the one before it; the page holds its buttons back as long, and a key
// that comes meanwhile does nothing here at all.

import { byId, markTheme, showText } from "./ui.js";

interface Content {
  title: string;
  lead: string;
  details: Array<{ label: string; value: string; code: boolean; keep: string }>;
  notes: string[];
  choice: { legend: string; options: Array<{ value: string; label: string; description: string }>; value: string } | null;
  buttons: Array<{ id: string; label: string; allows: boolean }>;
  focus: string;
  cancel: string;
  enter: string | null;
}

interface State {
  content: Content;
  waiting: number;
  armed: boolean;
  protection: number;
}

interface Prompt {
  state(): Promise<State>;
  answer(button: string, choice: string | null): Promise<boolean>;
  onChanged(listener: () => void): () => void;
  onArmed(listener: (armed: boolean) => void): () => void;
}

const prompt = (globalThis as unknown as { surogatePrompt: Prompt }).surogatePrompt;

markTheme();

let content: Content | null = null;
// Whether the prompt has been shown, or focused again, for the input protection: as the main process says.
let armed = false;
// The input protection, in milliseconds; when a key last went down here, or a press began; and whether that
// one acts: it came once the prompt was armed, and a protection time after the one before it. One that does
// not act does nothing: it moves no focus, changes no choice and answers nothing. The main process counts the
// same keys and presses by itself, and takes an answer from none but one that acts.
let protection = 500;
let lastDown = Number.NEGATIVE_INFINITY;
let acts = false;
let quiet: number | undefined;
const held = (): boolean => !armed || performance.now() - lastDown < protection;

const element = <K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string): HTMLElementTagNameMap[K] => {
  const made = document.createElement(tag);
  made.className = className;
  if (text !== undefined) showText(made, text);
  return made;
};

const chosen = (): string | null => document.querySelector<HTMLInputElement>("#prompt-choice input:checked")?.value ?? null;

function answer(id: string): void {
  const offered = content?.buttons.find((button) => button.id === id);
  if (!offered || !acts) return;
  void prompt.answer(id, content?.choice ? chosen() : null);
}

// Every button is held back until the input protection has passed, since the prompt was shown and since the
// last key or press: focusable, and marked unavailable.
function hold(): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>("#prompt-buttons button")) button.setAttribute("aria-disabled", String(held()));
  // Looked at again once what is left of the protection since the last key or press has passed.
  clearTimeout(quiet);
  const left = lastDown + protection - performance.now();
  if (armed && left > 0) quiet = window.setTimeout(hold, left + 1);
}

// A key went down, or a press began: it acts only where the prompt was not held back, and holds it back anew.
// A key held down repeats after a wait of its own, which can outlast the protection: it never acts.
function down(event: Event): void {
  acts = !held() && !(event instanceof KeyboardEvent && event.repeat);
  lastDown = performance.now();
  hold();
  if (!acts) still(event);
}
// What a key or a press that does not act would do next does not happen either: its key coming up, its click.
// A key does nothing at all: keys are what a person typing elsewhere sends here. But for the clipboard's and
// the selection's own keys, which answer nothing and move nothing: what a prompt shows can be copied at any
// time. A press of the mouse is made where it lands, and one that does not act is held back from the buttons
// alone: a choice it lands on is taken.
const CLIPBOARD = new Set(["a", "c", "v", "x"]);
function still(event: Event): void {
  if (event instanceof KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && CLIPBOARD.has(event.key.toLowerCase())) return;
  } else if (!(event.target instanceof Element && event.target.closest("#prompt-buttons"))) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}
document.addEventListener("keydown", down, true);
document.addEventListener("pointerdown", down, true);
for (const after of ["keyup", "click"]) {
  document.addEventListener(after, (event) => {
    if (!acts) still(event);
  }, true);
}

// More may wait than the line here holds: each chat keeps its own later prompts back until this one is answered.
function waiting(count: number): void {
  byId("prompt-waiting").textContent = count === 0 ? "" : "More prompts wait after this one.";
}

function draw(state: State): void {
  content = state.content;
  armed = state.armed;
  protection = state.protection;
  showText(byId("prompt-title"), content.title);
  showText(byId("prompt-lead"), content.lead);
  byId("prompt-details").replaceChildren(...content.details.map((detail) => {
    const value = element("div", detail.code ? "value code" : "value");
    showText(value, detail.value, detail.keep);
    if (detail.code) {
      // Shown whole: a long command scrolls, and the keyboard reaches it.
      value.tabIndex = 0;
      value.setAttribute("role", "region");
      value.setAttribute("aria-label", detail.label);
    }
    const block = element("div", "detail");
    block.append(element("div", "label", detail.label), value);
    return block;
  }));
  byId("prompt-notes").replaceChildren(...content.notes.map((note) => element("p", "note", note)));
  const fieldset = byId("prompt-choice");
  fieldset.hidden = content.choice === null;
  if (content.choice) {
    showText(byId("prompt-legend"), content.choice.legend);
    const { value } = content.choice;
    fieldset.append(...content.choice.options.map((option) => {
      const radio = document.createElement("input");
      Object.assign(radio, { type: "radio", name: "choice", value: option.value, checked: option.value === value });
      // Named by its label, and described by its description.
      const description = element("span", "desc", option.description);
      description.id = `choice-${option.value}`;
      radio.setAttribute("aria-describedby", description.id);
      const label = element("label", "");
      label.append(radio, element("span", "name", option.label));
      const row = element("div", "option");
      row.append(label, description);
      return row;
    }));
  }
  byId("prompt-buttons").replaceChildren(...content.buttons.map((offered) => {
    const button = element("button", "btn", offered.label);
    button.type = "button";
    button.dataset.id = offered.id;
    button.addEventListener("click", () => answer(offered.id));
    return button;
  }));
  waiting(state.waiting);
  hold();
  const focus = content.focus === "choice" ? "#prompt-choice input:checked" : `#prompt-buttons button[data-id="${content.focus}"]`;
  document.querySelector<HTMLElement>(focus)?.focus();
}

document.addEventListener("keydown", (event) => {
  if (!content) return;
  if (event.key === "Escape") {
    event.preventDefault();
    answer(content.cancel);
  } else if (event.key === "Enter" && content.enter !== null && !(event.target instanceof HTMLButtonElement)) {
    event.preventDefault();
    answer(content.enter);
  }
});

prompt.onArmed((now) => {
  armed = now;
  hold();
});
prompt.onChanged(() => void prompt.state().then((state) => waiting(state.waiting)));
void prompt.state().then(draw);
