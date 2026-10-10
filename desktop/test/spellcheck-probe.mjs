// Run by this package's Electron as its main, in a session of the test's own (test/spellcheck.test.ts):
// the app's spelling (dist/shell/spellcheck.js), as its main sets it up before ready, from the
// dictionaries folder PROBE_SHIPPED names; without PROBE_SHIPPED, nothing of the app's, as Electron
// spell-checks by itself. Then a window of the default session and a view of an agent's partition,
// each with a field it spell-checks, as the app's window and its web client are. It prints, for each
// session, the languages it spell-checks and what its spellchecker did, and exits. PROBE_ASK names
// another address to ask a missing dictionary of, set after the app's own: the name it asks shows there.
import { app, BrowserWindow, WebContentsView } from "electron";

import { ownSpelling } from "../dist/shell/spellcheck.js";

app.setPath("userData", process.env.PROBE_USERDATA);
if (process.env.PROBE_SHIPPED) ownSpelling(app, process.env.PROBE_SHIPPED, (error) => console.error(error));

const EVENTS = ["spellcheck-dictionary-initialized", "spellcheck-dictionary-download-begin", "spellcheck-dictionary-download-success", "spellcheck-dictionary-download-failure"];
const ENDS = new Set(["spellcheck-dictionary-initialized", "spellcheck-dictionary-download-success", "spellcheck-dictionary-download-failure"]);
const heard = new Map();
const ended = new Map();
app.on("session-created", (made) => {
  const said = [];
  const { promise, resolve } = Promise.withResolvers();
  heard.set(made, said);
  ended.set(made, promise);
  if (process.env.PROBE_ASK) made.setSpellCheckerDictionaryDownloadURL(process.env.PROBE_ASK);
  for (const name of EVENTS) {
    made.on(name, (_event, language) => {
      said.push(`${name.replace("spellcheck-dictionary-", "")} ${language}`);
      if (ENDS.has(name)) resolve();
    });
  }
});

const FIELD = "data:text/html,<textarea spellcheck=true autofocus>Teh qiuck brwon fox</textarea>";
app.whenReady().then(async () => {
  const window = new BrowserWindow({ show: false });
  await window.loadURL(FIELD);
  const view = new WebContentsView({ webPreferences: { partition: "persist:agent" } });
  window.contentView.addChildView(view);
  await view.webContents.loadURL(FIELD);
  const sessions = { default: window.webContents.session, agent: view.webContents.session };
  // Each until its spellchecker has a dictionary, or has given up on one; at most ten seconds.
  await Promise.race([Promise.all(Object.values(sessions).map((made) => ended.get(made))), new Promise((done) => setTimeout(done, 10_000))]);
  const answers = Object.fromEntries(Object.entries(sessions).map(([name, made]) => [name, { languages: made.getSpellCheckerLanguages(), heard: heard.get(made) }]));
  console.log(`PROBE ${JSON.stringify(answers)}`);
  app.exit(0);
});
