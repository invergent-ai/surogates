// One of the desktop's prompts (spec, Section 4), drawn from what the main process
// sends. Every string is set as text, with each special character in it shown as its
// code point. The buttons answer the main process, which takes no answer before the
// input protection has passed, nor one from a key or a press that came sooner than
// that after the one before it; the page holds the same keys and presses back, and a
// key held back does nothing here but move the keyboard. A button pressed with no key
// and no press, as assistive technology presses one, answers once none has come for
// that long.

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
// The input protection, in milliseconds. *lastDown*: when a key last went down here, or a press began.
// *acts*: whether that one acts, having come once the prompt was armed and a protection time after the one
// before it; one that does not act changes no choice and answers nothing. *lastInput*: when a key or a press
// last went down or came up. *pressed*: the key or press that went down last and has not come up. The main
// process counts the same keys and presses by itself, and decides by its own count.
let protection = 500;
let lastDown = Number.NEGATIVE_INFINITY;
let lastInput = Number.NEGATIVE_INFINITY;
let acts = false;
let pressed: string | null = null;
// The keys that went down held back and have not come up: their coming up does nothing either.
const stilled = new Set<string>();
let quiet: number | undefined;
// A key or a press that came now would be held back.
const held = (): boolean => !armed || performance.now() - lastDown < protection;
// A button pressed now answers: by the key or press that acts, or with none at all for a protection time.
const answering = (): boolean => armed && (acts || performance.now() - lastInput >= protection);

const element = <K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string): HTMLElementTagNameMap[K] => {
  const made = document.createElement(tag);
  made.className = className;
  if (text !== undefined) showText(made, text);
  return made;
};

const chosen = (): string | null => document.querySelector<HTMLInputElement>("#prompt-choice input:checked")?.value ?? null;

function answer(id: string): void {
  const offered = content?.buttons.find((button) => button.id === id);
  if (!offered || !answering()) return;
  void prompt.answer(id, content?.choice ? chosen() : null);
}

// Whether a key or a press would be held back now is said on the buttons' row, for how they are drawn; no
// button is marked unavailable for it: each can be reached and pressed at any time, and what is pressed too
// soon answers nothing.
function hold(): void {
  const row = byId("prompt-buttons");
  row.dataset.held = String(held());
  row.dataset.armed = String(armed);
  // Looked at again once what is left of the protection since the last key or press has passed.
  clearTimeout(quiet);
  const left = lastDown + protection - performance.now();
  if (armed && left > 0) quiet = window.setTimeout(hold, left + 1);
}

// The keys that only change what another key means: none of them is a key that holds anything back.
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "AltGraph"]);
const named = (event: Event): string => (event instanceof KeyboardEvent ? event.code || event.key : "press");

// A key went down, or a press began: it acts only where the prompt was not held back, and holds it back anew.
// A key held down repeats after a wait of its own, which can outlast the protection: its repeats are the
// press that began it, and act only where that one was seen to begin here and acted.
function down(event: Event): void {
  if (event instanceof KeyboardEvent && MODIFIERS.has(event.key)) return;
  const repeated = event instanceof KeyboardEvent && event.repeat;
  acts = repeated ? acts && pressed === named(event) : !held();
  if (!repeated) pressed = named(event);
  lastDown = lastInput = performance.now();
  hold();
  if (acts) return;
  if (event instanceof KeyboardEvent) stilled.add(named(event));
  still(event);
}
function up(event: Event): void {
  if (event instanceof KeyboardEvent && MODIFIERS.has(event.key)) return;
  if (pressed === named(event)) pressed = null;
  lastInput = performance.now();
}
// What a key or a press that does not act would do next does not happen either: its key coming up, its click.
// A key changes nothing and answers nothing: keys are what a person typing elsewhere sends here. But for Tab,
// which moves the keyboard as in any window, and for the clipboard's and the selection's own keys: what a
// prompt shows can be read and copied at any time. A press of the mouse is made where it lands, and one that
// does not act is held back from the buttons alone: a choice it lands on is taken.
const CLIPBOARD = new Set(["a", "c", "v", "x"]);
function still(event: Event): void {
  if (event instanceof KeyboardEvent) {
    if (event.key === "Tab") return;
    if ((event.ctrlKey || event.metaKey) && !event.altKey && CLIPBOARD.has(event.key.toLowerCase())) return;
  } else if (!(event.target instanceof Element && event.target.closest("#prompt-buttons"))) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}
document.addEventListener("keydown", down, true);
document.addEventListener("pointerdown", down, true);
// A click with a key or a press behind it goes by that one; with none, by how long none has come. Looked at
// before the key's or the press's coming up is counted: the click that ends a press is that press's.
for (const after of ["keyup", "click"]) {
  document.addEventListener(after, (event) => {
    if (event instanceof KeyboardEvent && MODIFIERS.has(event.key)) return;
    if (event instanceof KeyboardEvent ? stilled.delete(named(event)) : !answering()) still(event);
  }, true);
}
for (const after of ["keyup", "pointerup", "pointercancel"]) document.addEventListener(after, up, true);

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
