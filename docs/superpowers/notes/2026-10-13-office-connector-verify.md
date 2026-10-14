# Office editors: verify results for live agent editing

Run on 2026-10-13 against the DEV document server (Euro-Office 9.3.5.37, `https://office.densemax.local`) and a local
container of the same image, with Playwright (playwright-core 1.60.0, Chromium build 1223) driving two editor pages on
one document key: an agent page and a user page. This note answers the spec's verify items 2 and 4–10, which decide the
live agent editing plan. The spike's scripts were throwaway and are not kept; every working snippet is copied below.

## Verify item 2: one history entry for an editor save, with its author

**Partly, with no change to `BucketHistory`.** A cloud project's editor save can be recorded as one history entry by
"you" or by a thread: under the project's lock, settle running landings, write the file, then run the pickup step
`undo._pick_up(..., you=<author>, paths=[path])`. Its author is a `{"name", "email"}` dict; an email `thread:<id>@surogate`
reads as that thread, anything else as you (`surogates/sandbox/history.py` `_who`). There is no "both": the wire type
`ChangedBy` is `you | thread | routine`. A pickup entry is not undoable and does not count on a thread's card.
The write must happen inside the lock, or a thread's landing can pick the save up first, as you.

---

## Where this ran

- **DEV** `https://office.densemax.local` (9.3.5.37, cluster-docs image): every script was re-run here once the VPN came back.
  Logs are in `dev-logs/*.log`. Run them with `./run_dev.sh` (sources `dev.env.sh`).
  - **Every qualitative result below was reproduced on DEV.** The only differences are timings: forcesave and status 2 are slower
    on DEV. DEV numbers are given where they differ.
- **Local** container `ghcr.io/euro-office/documentserver:v9.3.5` (also 9.3.5.37, all-in-one image) on `localhost:8790`.
  The VPN (`vpn0`, NetworkManager openconnect) was down from 16:34 until about 18:15, so DEV could not be reached. The numbers in
  this file come from local runs unless they are marked DEV.
- **VPN address changed.** The workstation is now `172.17.32.40`, not `.39`. `dev.env.sh` reads the address from `vpn0`.
- **Client side:** playwright-core 1.60.0 with the cached chromium-1223 headless shell. Test doc: `files/test.docx`, about 8 pages
  ("Page 1 of 8"), 101 top-level blocks: H1–H3, 48 list items, a table, an image.
- **Editors:** an agent page (`agent:spike` / "Agent") and a user page (`user:spike` / "Flavius"), each in its own browser
  context, on the same random 32-hex key.


## Summary

| Item | Answer | One-line evidence |
|---|---|---|
| **V4** flush signal | **yes** | `onDocumentStateChange(false)` on the agent's host page fires about 5–60 ms after the command returns. It means "all local changes sent and acknowledged". A connector command can also read it with `Api.GetDocument().Document.Api.isDocumentModified()`. |
| **V4** forcesave + userdata | **yes** | `/command {c:"forcesave", key, userdata}`. The status 6 callback arrives in about 0.6 s locally (2.5–4.4 s on DEV), with `userdata` echoed verbatim (also inside the callback JWT) and `forcesavetype: 0`. A forcesave sent before the flush returns `error: 4` (no changes) and no save happens. |
| **V5** undo of a partial batch | **yes** | One command is one history point. A single connector `method: "Undo"` reverts both partial changes for the agent and the user, and leaves the user's interleaved edit alone. `StartAction/EndAction("GroupActions")` is a real transaction with commit and cancel. |
| **V6** track changes | **yes** | After the user turns on "ON for me and everyone" (UI call or connector `SetTrackRevisions(true)`), the agent's edits are tracked. The saved docx has `<w:ins w:author="Agent">` ×4 and `<w:del w:author="Agent">` ×1. `GetReviewReport()` returns `{"Agent":[…]}`. |
| **V7** memory | **measured** | One context with an open editor: renderer about 440–630 MB RSS (PSS 350–560). Marginal cost per extra context: about 440–670 MB PSS, including browser (+55 MB), GPU (+30 MB) and network (+20 MB) growth. JS heap is only 55 MB used / about 100–117 MB total. An 800×600 viewport saves only about 5–10%. |
| **V8** stable ids | **yes in-session / no across reopen** | `GetInternalId()` is identical on both pages (load ids like `"853"`; created ids like `"1_1"` / `"2_6"`, prefixed by the creator's index). Ids survive the other editor's edits. Split: the first half keeps the id. Merge: the surviving paragraph keeps its id. Reopen: all internal ids change. **But** `GetParaId()`/`SetParaId()` (w14:paraId) survive reopen (4/4). `Api.GetByInternalId(id)` looks up an element by id. |
| **V9** pending content controls | **yes** | The placeholder shows on the user page (screenshot; `Sdt.IsPlaceHolder()` true). `GetContentControlsByTag("pending:k")` finds it, and unlock + `RemoveAllElements` + `AddElement` + `Delete(true)` fills and unwraps it. When the user deletes it, find returns `[]`. A `contentLocked` control cannot be typed into, but one Delete removes it. |
| **V10** plugins disabled | **yes, still works** | `customization.plugins:false` (sdkjs `asc_getIsEnabledPlugins()` is false) with or without `editorConfig.plugins` empty: command and method both work. |
| **(a)** list blocks | **yes** | `GetElement(i)`, `GetClassType`, `GetStyle().GetName()`, `GetNumbering()`, `GetText({Numbering})`, `GetAllDrawingObjects`, and for content controls `GetTag` / `GetPlaceholderText`. |
| **(b)** insert content | **yes** | `SetStyle(doc.GetStyle("Heading N"))`. Lists use style "List Bullet" / "List Number", optionally with `SetNumbering(existingPara.GetNumbering())` (continues the existing list: 4.). `CreateNumbering` starts a fresh list at 1. Tables via `Api.CreateTable` + `doc.AddElement`. |
| **(c)** replace in place | **yes (range method); `SearchAndReplace` NO with co-editors** | The range method `AddText(new,"after")` + `GetRange(start,start+len).Delete()` keeps formatting (bold stays bold). `ApiDocument.SearchAndReplace` works with one editor but **silently fails or throws when a second editor is connected**. |
| **(d)** comment + reply | **yes** | `range.AddComment(text, author, userId)` then `comment.AddReply(text, author, userId)`. The user sees the comment, the quote and the reply, all authored "Agent". |

## Connector protocol (re-confirmed on 9.3.5)

- **Sending:** post straight to the editor iframe with `iframe.contentWindow.postMessage(JSON.stringify(msg), "*")`. Replies arrive on the host window.
- **Code shape:** command code runs through `safePluginEval`. It runs in strict mode, and `window`, `document`, `XMLHttpRequest`, `self`,
  `globalThis` and `Function` are shadowed with `{}`. Real globals are still reachable by bare name: `AscCommon` and `Api` work.
- **Exceptions:** an exception is swallowed. `commandReturnData` comes back `undefined` and the partial edits stay.
  Always wrap the body in `try/catch` and return `{__error: String(e)}`.
- **Return values** must pass `Asc.checkReturnCommand`: primitives, arrays and plain objects, depth ≤ 10. Builder objects do not pass.
- **Replies are FIFO.** There is no request id, so serialize commands per page.

```js
// host.html (excerpt)
const conn = {
  guid: "asc.{" + crypto.randomUUID() + "}", queue: [],
  frame() { return document.querySelector("iframe[name=frameEditor]"); },
  post(data) { this.frame().contentWindow.postMessage(JSON.stringify({type: "onExternalPluginMessage", subType: "connector", data}), "*"); },
  register() { this.post({type: "register", guid: this.guid}); },            // after onDocumentReady
  send(data) { return new Promise((resolve) => { this.queue.push(resolve); this.post(data); }); },
  command(fnSrc, scope, recalculate = true) {
    const code = "var Asc = {}; Asc.scope = " + JSON.stringify(scope || {}) + "; var scope = Asc.scope; (" + fnSrc + ")();";
    return this.send({type: "command", guid: this.guid, data: code, recalculate});
  },
  method(methodName, args) { return this.send({type: "method", guid: this.guid, methodName, data: args || []}); },
};
window.addEventListener("message", (e) => {
  let m = e.data; if (typeof m === "string") { try { m = JSON.parse(m); } catch { return; } }
  if (!m || m.type !== "onExternalPluginMessageCallback" || m.data?.guid !== conn.guid) return;
  if (m.data.type === "onCommandCallback" || m.data.type === "onMethodReturn")
    conn.queue.shift()?.(m.data.type === "onCommandCallback" ? m.data.commandReturnData : m.data.methodReturnData);
});
```

**Direct frame access.** runtime-office owns the browser, so `frame.evaluate()` inside the `frameEditor` iframe can call the
editor's internal API directly (`editor.isDocumentModified()`, `editor.asc_SetGlobalTrackRevisions()`, …). These are internal names
and not a stable contract. I used them only for diagnosis and to replicate the toolbar's track-changes call.

---

## V4: flush and forced save

### Answer
**Yes.** The agent's host page gets `onDocumentStateChange` from the DocEditor `events`.

- **When to forcesave:** wait for the first `false` *after* the command was sent, then send the forcesave.
- **What `false` means:** the editor's `isDocumentModified()` dropped to false. That happens once the changes were sent to the
  server and acknowledged (`canSave` back to true).
- **Read-only commands** produce no event, and need no flush.
- **Without the event:** the same flag can be polled from a connector command.

### Evidence

**Event sequence** around one connector edit (`v4_flush.js`). The `t` values are ms after the command was sent.

| Run | Command returned | First `true` | `false` (flush done) | User page saw the edit |
|---|---|---|---|---|
| Local | +544 | +526 | +583 | +1095 |
| DEV | +775 | +759 | +825 | +1485 |

- Internal transitions match (`mod=true canSave=false` → `mod=false canSave=true`).
- The user page never fires `onDocumentStateChange`. Only the editor that made the change does.
- **Forcesave latency** (`v4b_latency.js`, 6 back-to-back iterations):

  | | Flush wait | `/command` answers in | Status 6 callback after the request |
  |---|---|---|---|
  | Local | 40–70 ms after the command returns (366 ms the first time) | 7–9 ms | **598–638 ms** (885 ms the first time) |
  | DEV | 60–117 ms (1142 ms the first time) | 16–102 ms | **2.7 s rising to 4.4 s** over 6 consecutive forcesaves of one key (2.5 s in `v4_flush`) |

  - The DEV numbers rise from one forcesave to the next, which looks like queueing in the converter. Budget about 3–5 s per
    forcesave on DEV, and don't fire them back to back.
- **Forcesave with no pending changes:** `{"error":4}`, no callback.
- **Forcesave sent the moment the command returned** (before the flush): `{"error":4}` and **no save**. A retry after the flush:
  `error 0`, the callback arrives, and the file contains the edit.
- **Status 6 callback body** (url, changesurl, history and token removed):
  `{"key":"…","status":6,"users":["agent:spike"],"userdata":"{\"op\":\"v4-after-flush\",\"n\":1}","lastsave":"…","forcesavetype":0,"filetype":"docx"}`.
  - The body carries `token` (HS256 over the same payload, including `userdata`). It verified with the server secret.
  - `url` points at the document server's cache. The downloaded file contained the marker.
- **Status 2** arrives about 5.7 s after the last editor disconnects locally (9.8 s on DEV). `users:["agent:spike"]`, no `userdata`.
- **`info` command:** `{c:"info", key, userdata}` returns `{"error":0,"users":["agent:spike","user:spike"]}` and also triggers a
  status 1 callback carrying `userdata`.
- **Command Service auth:** both paths, `/command` and `/coauthoring/CommandService.ashx`, accept the JWT in the body (`token`) or as
  `Authorization: Bearer`. An unsigned request gets `{"error":6}`.

### Code
```js
// wait for flush (host page; events[] filled by DocEditor events.onDocumentStateChange)
async function waitFlushed(ed, since, timeoutMs = 10000) {
  return ed.page.evaluate(([since, timeoutMs]) => new Promise((resolve) => {
    const t0 = Date.now();
    (function poll() {
      const sc = window.events.filter((e) => e.name === "onDocumentStateChange" && e.t >= since);
      const last = sc[sc.length - 1];
      if (last && last.data === false) return resolve(last.t - since);
      if (Date.now() - t0 > timeoutMs) return resolve(-1);
      setTimeout(poll, 20);
    })();
  }), [since, timeoutMs]);
}
```
```js
// alternative: read the flag from a connector command (internal name, works on 9.3.5)
function () { return Api.GetDocument().Document.Api.isDocumentModified(); }   // false = nothing unsent
// note: AscCommon.History.Have_Changes() is always true inside a command (the command is itself an action) - useless
```
```js
// Command Service forcesave (JWT HS256 over the payload, in body `token` or Authorization: Bearer)
const payload = { c: "forcesave", key, userdata: JSON.stringify({ op: "…", n: 1 }) };
await fetch(`${DS}/command`, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ ...payload, token: jwtHS256(payload, secret) }) });
// -> {"key":"…","error":0}  then callback status 6 with the same userdata; error 4 = nothing to save
```

**Design note.** `error:4` means "the server has no unsaved changes". If you send forcesave before the flush, you get error 4 even
though an edit is in flight, and nothing will ever save it. Always: command, then `waitFlushed`, then forcesave. Treat error 4 after
a flush as "already saved".

---

## V5: undo of a partially applied batch

### Answer
**Yes.** Two mechanisms work, for both editors.

1. **Single `Undo` method after a failed command.** `canRunBuilderScript` wraps every command in
   `StartAction(BuilderScript)`…`FinalizeAction`, so each command is exactly one history point. A single connector
   `{"type":"method","methodName":"Undo","data":[]}` reverts the whole command, including both partial changes, on the agent page and
   on the user page. Undo in fast co-editing only reverts the agent's own changes: a user edit made in between survives.
2. **`GroupActions` transaction.**
   - Open it with `method StartAction ["GroupActions"]`.
   - Commit with `EndAction ["GroupActions", ""]`, or roll back with `EndAction ["GroupActions", "", "cancel"]`.
   - While the group is open, the agent's changes are **held locally**: the user does not see them. Incoming user changes are
     **deferred** too: the agent page does not see them until the group ends.
   - Cancel undoes every point since the group opened. The co-editors never saw those changes. The user's concurrent edits are kept
     and both pages converge.
   - Commit sends everything. One `Undo` afterwards reverts the whole group.

### Evidence (`v5_undo.js`, `followups.js`)
**Single Undo:**
- A throwing command returns `commandReturnData: undefined`. PARTIAL text is present on both pages.
- `CanUndo` = true. After one `Undo`, agent == base and user == base. `CanUndo`/`CanRedo` are then false.
- Wrapped command → `{"__error":"Error: boom after two changes"}`. The user edits paragraph 5, then the agent runs `Undo`: PARTIAL
  is gone on both pages, USER-EDIT is kept on both, and the pages are equal.

**GroupActions:**
- During the group, the user does not see GROUP-1. After cancel, both pages == base.
- Commit: the user sees both changes, and one `Undo` returns to base.
- With a concurrent user edit during the open group:
  - The agent does not see the user's edit until the group ends.
  - Commit: the pages converge with all three edits.
  - Cancel: the pages converge, the agent's edit is gone and the user's edit is kept.

### Code
```js
// command wrapper: report failures instead of letting safePluginEval swallow them
function () {
  try { /* ... edits ... */ return { ok: true }; }
  catch (e) { return { __error: String(e) }; }
}
// rollback a failed single command:
{ type: "method", guid, methodName: "Undo", data: [] }                    // also "CanUndo", "Redo"
// multi-command transaction:
{ type: "method", guid, methodName: "StartAction", data: ["GroupActions"] }
{ type: "method", guid, methodName: "EndAction",   data: ["GroupActions", ""] }          // commit
{ type: "method", guid, methodName: "EndAction",   data: ["GroupActions", "", "cancel"] } // rollback
```

**Caveats.**
- **Don't hold a group long.** It defers incoming changes and sets the agent page's global lock.
- **Undo is global to the agent page.** Use it only right after the agent's own command; the agent page has no human typing.

---

## V6: track changes ("ON for me and everyone")

### Answer
**Yes.** Once the user turns on tracking for everyone, the agent's connector edits arrive as tracked revisions authored by the
agent's `user.name` ("Agent").

There are two ways for the user to turn it on:
- **UI:** the toolbar's "ON for me and everyone" calls `asc_SetLocalTrackRevisions(null)` + `asc_SetGlobalTrackRevisions(true)`.
  I replicated those exact calls in the user frame.
- **Connector:** the user's connector runs `Api.GetDocument().SetTrackRevisions(true)`, which calls `SetGlobalTrackRevisions`.

The global flag goes into the document settings and propagates. After either call, both pages report
`{local:null, global:true, is:true}`, and the agent's `Api.GetDocument().IsTrackRevisions()` is true.

### Evidence (`v6_track.js ui` / `connector`, `v6_report.js`)
- **Saved docx (forcesave) after the agent's insert + in-place replace:** `{"del:Agent":1,"ins:Agent":4}`, and `settings.xml` has
  `<w:trackRevisions/>`. Same result for both modes, locally and on DEV.
- **DEV report for the same edits:** the in-place replace shows up as a proper insert/delete pair:
  `{"Agent":[{"Type":"ParaAdd"},{"Type":"TextAdd","Value":"AGENT TRACKED INSERT"},{"Type":"TextAdd","Value":"sales goal"},{"Type":"TextRem","Value":"revenue target"}]}`.
- **`GetReviewReport()` on the user page:**
  `{"Agent":[{"Type":"ParaAdd","para":"1_1"},{"Type":"TextAdd","Value":"AGENT TRACKED INSERT","para":"1_1"},{"Type":"TextAdd","Value":" +agent","para":"853"}]}`.
  - The report object has an extra enumerable `ToJSON` function key. Skip non-arrays.

### Code
```js
// user side (connector) - turn on for everyone
function () { return Api.GetDocument().SetTrackRevisions(true); }
// agent side - check / report
function () {
  var rr = Api.GetDocument().GetReviewReport(), out = {};
  for (var name in rr) if (Array.isArray(rr[name]))          // skip the "ToJSON" key
    out[name] = rr[name].map(function (x) { return { Type: x.Type, Value: x.Value, Date: x.Date, para: x.ReviewedElement ? x.ReviewedElement.GetInternalId() : null }; });
  return out;
}
```

**Design notes.**
- **Never put `customization.review.trackChanges` in the agent's editor config.** If it is set (true or false), web-apps calls
  `asc_SetLocalTrackRevisions(...)` on load, and the local flag overrides the global one.
- **Tracking the agent when the user hasn't asked (untested).** `ApiDocument.SetAssistantTrackRevisions(true, name)` exists for
  this; it also swaps the editor's user name and id.
- **Tracked deletions stay in the text**, so position arithmetic (`GetStartPos`) can drift. The in-place replace below still worked
  in track mode.

---

## V7: memory per editor page

### Setup
- headless shell (`--headless=old`), viewport 1400×900.
- The host page is on 127.0.0.1 and the editor on localhost. Playwright's defaults keep the editor iframe in the same renderer as
  the page: one renderer per context.
- Each stage: open the editor, wait 6 s, make one edit, wait 2 s. Measured with `/proc/<pid>/smaps_rollup` (`v7_memory.js`).

### Results

| Stage | Total RSS / PSS (MB) | Renderers (RSS/PSS MB each) | Marginal PSS (MB) |
|---|---|---|---|
| Browser only | 318 / 155 | none | none |
| 1 context, own doc | 1100 / 828 | 613/557 | 673 |
| 2 contexts | 1845 / 1444 | 614/533, 633/551 | 616 |
| 3 contexts | 2410 / 1880 | 438/347, 633/542, 631/540 | 436 |
| +1 context on the **same** doc as #1 | 3075 / 2417 | +565/469 | 537 |
| After closing context #3 | 2142 / 1611 | 442/351, 440/349, 565/474 | (−806) |

- **DEV run** (`dev-logs/v7_memory.log`): the same shape, with renderers at 434–632 MB RSS and marginal PSS of 688, 615, 416, and
  275 MB for the extra context on the same doc.
- **Renderer size over time:** each renderer settles from about 630 MB to about 440 MB RSS a few seconds after load (GC).
- **JS heap** (CDP `Performance.getMetrics`): `JSHeapUsedSize` about 50–55 MB, `JSHeapTotalSize` 95–117 MB, about 15.3k DOM nodes.
  Most of the renderer is not JS heap; probably wasm, font engine and canvas.
- **Viewport 800×600:** renderers 411–590 MB RSS, marginal PSS 388–632 MB, so only about 5–10% less.
- **Shared processes grow per context:** browser process about +55 MB, GPU process +15–30 MB, network service +20 MB.

**Capacity rule of thumb:** about 0.5–0.7 GB per open document (one context and page), on top of about 0.15 GB for the browser.

---

## V8: stable ids

### Answer
- **Within one editing session (same doc key): yes.** Paragraph, table and content-control internal ids are identical on the agent
  page and the user page:
  - Load-time ids are assigned in load order (`"853"`, `"857"`, …; the table is `"959"`).
  - Ids created in-session are `"<creatorIndex>_<n>"` (agent `"1_1"`, user `"2_6"`) and propagate unchanged.
- **Nearby edits:** ids survive the other editor's appends, inserts before/after, and keyboard typing in the paragraph. Only deleted
  blocks lose their id.
- **Split** (Enter mid-paragraph): the first half keeps the id and the second half gets a new one.
- **Merge** (Backspace at the start of a paragraph): the surviving, preceding paragraph keeps its id and the merged-away id
  disappears.
- **Across reopen (new key): internal ids do not survive** (0/103 equal), as expected.
  **But `ApiParagraph.GetParaId()`/`SetParaId(n)` (w14:paraId) do survive** (4/4 non-zero paraIds identical after forcesave and
  reopen):
  - Paragraphs created in the editor get a random durable paraId automatically.
  - Paragraphs loaded from a file without `w14:paraId` (python-docx) have `GetParaId() == 0`, until someone calls `SetParaId`
    (0 < n < 0x80000000).
  - `SetParaId` is a normal, history-tracked edit, and the user page sees the new value.
  - Word-authored files normally carry w14:paraId on every paragraph.
- **Lookup by id:** `Api.GetByInternalId(id)` works for paragraph, table and content control, and returns null for unknown ids.

### Evidence
- **`v8_ids.js` (local):**
  - Initial: 101/101 blocks, 0 diffs between the pages.
  - After the agent insert + `SetParaId(0x1234567)`: 0 diffs; the new paragraph is `1_1` / paraId 557046440 on both pages; block 6's
    paraId is 19088743 on both.
  - After the user's edits: 0 diffs. Lost: `[["883","Subsection 1.1"]]`, the one the user deleted. Added: `2_6` (Enter), `2_2`
    (inserted).
  - Reopen: internal ids are all new (`1034`, `1038`, …); paraIds `2077497634`, `557046440`, `19088743` are preserved.
- **`v8b_split_merge.js`:**
  - Before: `867:[S1P0] Lorem…, 871:[S1P1]…`
  - After a real Enter after "[S1P0]": `867:[S1P0]`, `2_1: Lorem ipsum…`, `871:…`
  - After Backspace at the start of 871: 871 gone, merged into `2_1`.

### Code
```js
function () {   // ids for every top-level block
  var doc = Api.GetDocument(), out = [];
  for (var i = 0; i < doc.GetElementsCount(); i++) {
    var el = doc.GetElement(i);
    out.push({ id: el.GetInternalId(), paraId: el.GetClassType() === "paragraph" ? el.GetParaId() : null });
  }
  return out;
}
function () { return Api.GetByInternalId(scope.id) ? Api.GetByInternalId(scope.id).GetPosInParent() : -1; }
function () { Api.GetDocument().GetElement(scope.i).SetParaId(scope.paraId); return true; }   // durable id (w14:paraId)
```

**Design implications.**
- Internal ids are good session-scoped anchors (one doc key).
- To get anchors that survive close/reopen, stamp paraIds. They are paragraph-only; a table can be anchored through a cell
  paragraph.
- Ids on load are deterministic by load order, so two editors opening the same file agree even before any edit.

---

## V9: content controls as pending blocks

### Answer
**Yes.**
- `Api.CreateBlockLvlSdt()` tagged `pending:<key>` with `SetPlaceholderText(intent)` shows the intent text on the user page. Optional
  `SetBackgroundColor` / `SetBorderColor` make it visually distinct (see the screenshots).
- Later, `doc.GetContentControlsByTag("pending:<key>")` finds it, and the agent replaces its content and unwraps it
  (`Delete(true)` keeps the content).
- If the user deletes it, the lookup returns `[]`.

**What the user can do to each lock type** (`v9b_pending_ui.js`, real keyboard on the user page):

| Lock | User typing | User Delete |
|---|---|---|
| none | Replaces the placeholder (text becomes "USERTEXT"; the control stays) | First Delete on the selected content restores the placeholder; second Delete removes the control. |
| `contentLocked` | Blocked: the placeholder stays | **One Delete removes the whole control.** Best fit for "pending". |
| `sdtLocked` | Allowed | Delete is blocked |

- **The agent must unlock first.** Filling a `contentLocked` control without unlocking is silently ignored (the text stays the
  placeholder). With `SetLock("unlocked")` in the same command it works (`followups.js`).
- **Ctrl+A in the editor selects the whole document**, even inside a control. With an unlocked control, Delete then wipes the
  document; with `sdtLocked`, the deletion is blocked.

### Evidence
- User page: `{"type":"blockLvlSdt","id":"1_1","tag":"pending:k1","placeholder":"Agent will write the executive summary here","text":"Agent will write the executive summary here\r\n"}`.
  Internal `IsPlaceHolder()` is true; see `v9_user_placeholder.png`.
- After fill + unwrap, the user sees `heading2 "Executive summary"`, `paragraph "Revenue grew in all regions."`, and find returns
  `[]`.
- After the user deletes k2: the agent's find returns `[]`, and fill returns `{"error":"gone"}`.

### Code
```js
function () {   // create
  var sdt = Api.CreateBlockLvlSdt();
  sdt.SetTag("pending:" + scope.key);
  sdt.SetAlias("Agent is writing");
  sdt.SetPlaceholderText(scope.intent);
  sdt.SetLock("contentLocked");                    // user can delete it but not type into it
  sdt.SetBackgroundColor(230, 240, 255); sdt.SetBorderColor(80, 120, 200);
  Api.GetDocument().AddElement(scope.pos, sdt);
  return sdt.GetInternalId();
}
function () {   // fill and unwrap
  var sdt = Api.GetDocument().GetContentControlsByTag("pending:" + scope.key)[0];
  if (!sdt) return { error: "gone" };              // user deleted it
  sdt.SetLock("unlocked");
  sdt.RemoveAllElements();
  var h = Api.CreateParagraph(); h.AddText(scope.heading); h.SetStyle(Api.GetDocument().GetStyle("Heading 2"));
  var p = Api.CreateParagraph(); p.AddText(scope.body);
  sdt.AddElement(h, 0); sdt.AddElement(p, 1);
  return { unwrapped: sdt.Delete(true) };          // keepContent = true
}
```

**Gotcha.** While a control shows its placeholder, `GetRange().GetText()` returns the placeholder text. There is no exported
"is showing placeholder" getter. Use `sdt.Sdt.IsPlaceHolder()` (internal, reachable) or compare the text with
`GetPlaceholderText()`.

---

## V10: plugins disabled

### Answer
**Yes, the connector still works.** With plugins disabled, the editor still registers the connector, and commands and methods
behave as usual. I tested four configs (`v10_noplugins.js`); all four returned command `102` and method `GetVersion` → `"9.3.5"`:

| Config | sdkjs `DocInfo.asc_getIsEnabledPlugins()` |
|---|---|
| Default | true |
| `customization.plugins:false` + `editorConfig.plugins:{autostart:[],pluginsData:[]}` | **false** |
| `customization.plugins:false` alone | false |
| `editorConfig.plugins:{}` alone | true |

- **Why it works:** the connector is registered through `externalConnectorMessage → loadExtensionPlugins` (an "extension plugin"),
  which is independent of the plugins flag.
- **Untested:** the local image has an empty `sdkjs-plugins/`, and DEV has `PLUGINS_MARKET_DISABLED=true`. Neither disables the
  connector.

---

## (a)–(d) Office JS code (all work on 9.3.5, local and DEV)

### (a) List top-level blocks with kind and text (`cmds.js listBlocks`)
```js
function () {
  var doc = Api.GetDocument(), out = [];
  for (var i = 0, n = doc.GetElementsCount(); i < n; i++) {
    var el = doc.GetElement(i), t = el.GetClassType();
    var o = { i: i, type: t, id: el.GetInternalId() };
    if (t === "paragraph") {
      var st = el.GetStyle();                           // ApiStyle or null (Normal paragraphs -> null)
      o.style = st ? st.GetName() : null;
      o.paraId = el.GetParaId();
      var lvl = el.GetNumbering();                      // ApiNumberingLevel or null
      if (lvl) o.list = { level: lvl.GetLevelIndex(), num: el.GetText({ Numbering: true }).slice(0, 4) };
      var imgs = el.GetAllDrawingObjects();
      o.text = el.GetText({ Numbering: false });
      var m = o.style && /^heading (\d)$/i.exec(o.style);
      o.kind = m ? "heading" + m[1] : (o.list ? "listItem" : (imgs.length && !o.text.trim() ? "image" : "paragraph"));
    } else if (t === "table") {
      o.kind = "table"; o.rows = el.GetRowsCount(); o.text = el.GetRange().GetText();
    } else if (t === "blockLvlSdt") {
      o.kind = "contentControl"; o.tag = el.GetTag(); o.placeholder = el.GetPlaceholderText(); o.text = el.GetRange().GetText();
    }
    out.push(o);
  }
  return out;
}
```

**Results on the test doc:** `{"heading1":1,"paragraph":34,"heading2":8,"heading3":8,"listItem":48,"table":1,"image":1}`.

**Notes.**
- **Numbering text:** numbered items return `"1.\t…"`. Bullets return only `"\t"`: the Symbol-font bullet character is dropped. So
  tell bullet from numbered by style name, or by the absence of a number.
- **`doc.GetAllNumberedParagraphs()` excludes bullet paragraphs.**
- **Heading level:** use the style name. `GetOutlineLvl()` reads only direct paragraph properties.
- **Lines end in `\r\n`.** `GetText()` adds them to each paragraph.

### (b) Insert headings, lists using the document's numbering, and a table (`cmds.js insertContent`, `b_check.js`)
```js
function () {
  var doc = Api.GetDocument(), cur = doc.GetElement(scope.after);
  function para(text, style) { var p = Api.CreateParagraph(); p.AddText(text); p.SetStyle(doc.GetStyle(style)); return p; }
  var items = [para("Inserted H1", "Heading 1"), para("Inserted H2", "Heading 2"), para("Inserted H3", "Heading 3"),
               para("Bullet A", "List Bullet"), para("Step one", "List Number")];
  // join an existing list explicitly (continues its numbering): p.SetNumbering(existingListPara.GetNumbering())
  // fresh list restarting at 1: var num = doc.CreateNumbering("numbered"); p.SetNumbering(num.GetLevel(0));
  var tbl = Api.CreateTable(2, 2); tbl.SetStyle(doc.GetStyle("Table Grid"));
  tbl.GetCell(0, 0).GetContent().GetElement(0).AddText("h1");   // ... other cells
  items.push(tbl);
  for (var k = 0; k < items.length; k++) {
    if (items[k].GetClassType() === "paragraph" && cur.GetClassType() === "paragraph") cur.InsertParagraph(items[k], "after", true);
    else doc.AddElement(cur.GetPosInParent() + 1, items[k]);
    cur = items[k];
  }
  return true;
}
```

**Results:**
- The user page sees H1/H2/H3 with the right styles, bullets ("List Bullet" style-linked numbering), numbers ("1.", "2."), and the
  2×2 table, all with new ids `1_1`…`1_29`.
- `b_check.js`:
  - Joining the existing numbered list after item 3 gives "4. JOINED number".
  - Joining the existing bullet list works.
  - `CreateNumbering("numbered")` gives a fresh "1. / 2.".

### (c) Replace a found phrase in place, keeping formatting (`cmds.js replacePhrase`)
```js
function () {
  var doc = Api.GetDocument();
  var r = doc.Search(scope.find, true)[0];            // (text, matchCase) -> ApiRange[]
  if (!r) return { found: 0 };
  var start = r.GetStartPos(), len = r.GetText().length;
  r.AddText(scope.replace, "after");                  // inserted inside the last run of the match -> inherits its formatting
  var old = doc.GetRange(start, start + len);
  if (old.GetText() !== scope.find) return { error: "position drift" };
  old.Delete();
  return { ok: true };
}
```

**Results:**
- **The range method works.** Before: `"revenue target"` bold. After: `"sales goal"` bold, on the agent page and the user page.
- **Don't do it the naive way.** `r.AddText(x,"before"); r.Delete()` deletes the wrong characters, because the range grows to
  include the inserted text.
- **`ApiDocument.SearchAndReplace({searchString, replaceString, matchCase})` works only with no co-editor** (and replaces all
  matches).
  - **With a second editor connected, it returns `undefined` or does nothing** (`c_sr.js coedit`, `c_sr2.js coedit`; reproduced
    twice).
  - **Don't use it in runtime-office.**

### (d) Comment anchored to words, plus a reply (`cmds.js addComment`)
```js
function () {
  var r = Api.GetDocument().Search(scope.find, true)[0];
  var c = r.AddComment(scope.text, scope.author, scope.userId);   // ApiComment
  c.AddReply(scope.reply, scope.author, scope.userId);
  return { id: c.GetId(), quote: c.GetQuoteText(), replies: c.GetRepliesCount() };
}
```

**Results:** returns `{"id":"1184659669","quote":"Anchor phrase","replies":1}`. The user page's `GetAllComments()` shows
`[{"text":"Agent comment","author":"Agent","quote":"Anchor phrase","replies":[{"text":"Agent reply","author":"Agent"}]}]`.

---

## Surprises that change the design

1. **Forcesave races the flush.** A forcesave sent right after the command returns gets `error 4` (nothing to save) and saves nothing.
   Gate every forcesave on `onDocumentStateChange(false)` (or `Document.Api.isDocumentModified() === false`).
2. **`SearchAndReplace` is broken while anyone else is connected.** Use the range method for (c).
3. **`GroupActions` is a real transaction** (held locally, commit or cancel, deferred incoming changes). Combined with the
   one-command-one-history-point rule, rollback is cheap: `Undo` or `EndAction(..., "cancel")`.
4. **Internal ids are identical across co-editors and stable under edits**, and `Api.GetByInternalId` exists. They are lost on
   reopen. `SetParaId` / `GetParaId` (w14:paraId) gives reopen-durable paragraph anchors, but files without paraIds load as 0.
5. **Memory is about 0.5–0.7 GB per open editor context.** A small viewport barely helps.
6. **The command sandbox is porous.** `AscCommon`, `Api.GetDocument().Document` (CDocument) and `.Document.Api` (the editor api) are
   reachable. That is handy for flags like `isDocumentModified` and `Sdt.IsPlaceHolder()`, but these are internal, unversioned
   names.
7. **The agent's editor config must not set `customization.review.trackChanges`.** Otherwise it ignores "ON for everyone".
8. **Small API gotchas:**
   - `GetReviewReport()` has a `ToJSON` key.
   - `GetAllNumberedParagraphs` skips bullets.
   - Bullet characters are missing from `GetText({Numbering:true})`.
   - The placeholder text is returned as content.
