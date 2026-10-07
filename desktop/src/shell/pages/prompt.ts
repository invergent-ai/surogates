// One of the desktop's prompts (spec, Section 4), drawn from what the main process
// sends. Every string is set as text, with each special character in it shown as its
// code point. The buttons answer the main process, which ignores one that allows
// anything before the input protection has passed; the page holds those back as long.

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
let armed = false;
// Presses on what allows that began before the input protection passed: the click that ends one answers nothing.
const early = new WeakSet<HTMLButtonElement>();

const element = <K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string): HTMLElementTagNameMap[K] => {
  const made = document.createElement(tag);
  made.className = className;
  if (text !== undefined) showText(made, text);
  return made;
};

const chosen = (): string | null => document.querySelector<HTMLInputElement>("#prompt-choice input:checked")?.value ?? null;

function answer(id: string): void {
  const offered = content?.buttons.find((button) => button.id === id);
  if (!offered || (offered.allows && !armed)) return;
  void prompt.answer(id, content?.choice ? chosen() : null);
}

// What allows is held back until the input protection has passed: focusable, and marked unavailable.
function hold(): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>("#prompt-buttons button")) {
    if (content?.buttons.find((offered) => offered.id === button.dataset.id)?.allows) {
      button.setAttribute("aria-disabled", String(!armed));
    }
  }
}

// More may wait than the line here holds: each chat keeps its own later prompts back until this one is answered.
function waiting(count: number): void {
  byId("prompt-waiting").textContent = count === 0 ? "" : "More prompts wait after this one.";
}

// Where a press on *button* begins: one on what allows, before the input protection has passed, answers nothing.
function begin(button: HTMLButtonElement): void {
  if (content?.buttons.find((offered) => offered.id === button.dataset.id)?.allows && !armed) early.add(button);
  else early.delete(button);
}

function draw(state: State): void {
  content = state.content;
  armed = state.armed;
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
    button.addEventListener("pointerdown", () => begin(button));
    button.addEventListener("click", () => {
      if (early.delete(button)) return;
      answer(offered.id);
    });
    return button;
  }));
  waiting(state.waiting);
  hold();
  const focus = content.focus === "choice" ? "#prompt-choice input:checked" : `#prompt-buttons button[data-id="${content.focus}"]`;
  document.querySelector<HTMLElement>(focus)?.focus();
}

document.addEventListener("keydown", (event) => {
  if (!content) return;
  // A key held down from before the prompt opened answers nothing.
  if (event.repeat && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    return;
  }
  // An Enter or a Space on a button begins a press there, whenever its key comes up.
  if ((event.key === "Enter" || event.key === " ") && event.target instanceof HTMLButtonElement) begin(event.target);
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
