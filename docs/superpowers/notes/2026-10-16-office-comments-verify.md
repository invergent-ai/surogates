# Office editors: verify results for comments that call the agent

Run on 2026-10-16 against the DEV document server (Euro-Office 9.3.5, `https://office.densemax.local`), with
runtime-office's own page code (`EditorPage`, the host page, the connector). It answers the spec's verify item 13 and
everything plan 5 needs to know about mentions, comment threads, and replying and resolving as the agent in all four
editors. The probe's scripts were throwaway and are not kept; every working snippet is copied below, with what came
back.

**How it was probed.** Two pages were opened on one document key:

- a **person page**, user `user:probe` ("Flavius"): the host page plus an `onRequestUsers` handler answering
  `docEditor.setUsers({c: event.data.c, users: [{id: "agent:probe", name: "Agent", email: "agent@agents.surogate.ai"}]})`
  and an `onRequestSendNotify` handler that records the event;
- an **agent page**, user `agent:probe` ("Agent"): the plain host page, driven by connector commands.

The person's comments and replies were all made through the **real UI**: select text or a cell, Collaboration tab →
Add Comment (or the popover's "Add reply"), type with `page.keyboard.type`, then click Add or Reply. Each editor was
also run as a **late join**: the person comments first and the agent page opens afterwards, as when the runtime wakes
the agent and then opens its page. `AscCommon.g_oTableId.m_bTurnOff` stayed `false` after every call below, including
the ones that threw.

## Verify item 13: what the editor sends on a mention

Read from the DEV web-apps code (`documenteditor` main `app.js`). `pickEMail(commentId, message, oldMessage)` runs on
adding a comment, changing a comment, adding a reply and changing a reply, and only for addresses newly added to the
text:

```js
Common.Gateway.requestSendNotify({emails: arr, actionId: commentId, actionLink: {action: {type: "comment", data: commentId}}, message})
```

- `commentId` is `ascComment.asc_getGuid()`, the comment's GUID; for a reply, its parent comment's GUID. In the PDF
  editor that GUID is made anew each time (PDF, section 3).
- A mention is matched by `/\B[@+][A-Z0-9._%+-]+@[A-Z0-9._-]+\.[A-Z]+\b/gi` only: whoever is mentioned needs an
  email-shaped address.
- The editor asks for users and sends notices only when the config's `events` carry `onRequestUsers` and
  `onRequestSendNotify` (`mode.canRequestUsers`, `mode.canRequestSendNotify`). Events are added by the browser and are
  not part of the signed config.

So yes: `onRequestSendNotify` carries the comment id in `actionLink.action.data`.

## What every editor showed

Per editor, the agent page's GUID matched the notify for the same comment, both live and on late join:

| editor | notify `actionId` | how to reach it from the agent page |
|---|---|---|
| word | 8 lowercase hex, e.g. `"15689582"` = durable id 359175554 in hex | `doc.GetCommentById(String(parseInt(actionId, 16)))`. The builder's `ApiComment.GetId()` returns the durable id in decimal. |
| cell | `"{F80AA885-C2F8-8A96-7622-6B76D3A85AB7}"` | `apiComment.Comment.asc_getGuid()` (internal `sGuid`) |
| slide | `"{9D24FA3F-798C-22EB-4B89-84EF55C02C52}"` | `apiComment.Comment.GetData().m_sGuid` (internal) |
| pdf | 8 hex, e.g. `"5885824a"`: **random every time it is built** | not reachable. Match on text instead (PDF, section 3). |

In every editor, a **reply's** notify carries the **parent comment's** id as `actionId`, and `message` holds the
reply's full text. No reply id is sent. `onRequestUsers` came only with `c: "info"` (at open and when a co-editor
joins). A `c: "mention"` request never came while typing `@…`, yet the notify still fires on the typed address.

---

## Word (docx)

### 1. Notify

UI: double-click "North", Collaboration → Add Comment, type, Add.

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"15689582","actionLink":{"action":{"type":"comment","data":"15689582"}},"message":"@agent@agents.surogate.ai please check this"}
```

Reply (UI "Add reply", typed, Reply):

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"15689582","actionLink":{"action":{"type":"comment","data":"15689582"}},"message":"thanks @agent@agents.surogate.ai, also the east"}
```

Editing a comment whose text already mentioned the agent fired no new notify.

### 2. List

On the agent page, ran as is:

```js
function () { try {
  return Api.GetDocument().GetAllComments().map(function (c) {
    var replies = [];
    for (var i = 0; i < c.GetRepliesCount(); i++) { var r = c.GetReply(i); replies.push({ author: r.GetAuthorName(), userId: r.GetUserId(), text: r.GetText() }); }
    return { thread: c.GetId(), guid: Number(c.GetId()).toString(16).padStart(8, "0"), author: c.GetAuthorName(), userId: c.GetUserId(), text: c.GetText(), quote: c.GetQuoteText(), time: c.GetTimeUTC(), resolved: c.IsSolved(), replies: replies };
  }); } catch (e) { return { error: String(e && e.stack || e) }; } }
```

→ `{thread:"359175554", guid:"15689582", author:"Flavius", userId:"user:probe", text:"@agent@… please check this",
quote:"North ", resolved:false, replies:[{author:"Agent", userId:"agent:probe", text:"On it."}, …]}`. The quote's
trailing space came from the double-click selection.

### 3. GUID

The web-apps getter is `asc_CCommentDataWord.asc_getGuid = m_nDurableId.toString(16).padStart(8,"0")`. In 9.3.5 the
builder's `ApiComment.GetId()` is `""+this.Comment.GetDurableId()`, **not** the internal object id (that is
`c.Comment.Id`, e.g. `"1_1"`). runtime-office's existing `thread` ids are therefore already durable ids, in decimal.
`GetCommentById(sId)` tries the durable id first, then the internal id. The durable id is also written to the file
(`c_oSer_CommentsType.DurableId`), so it should survive a save; that was not tested across a save.

### 4. Reply and resolve as the agent

runtime-office's own `changeThread`/`replyOf` path, keyed by the GUID:

```js
function () { try {
  var doc = Api.GetDocument();
  function changeThread(thread, change) {
    var found = doc.GetCommentById(String(thread));
    if (!found) throw new Error("gone");
    var data = found.Comment.GetData().Copy();
    change(data);
    doc.Document.EditComment(found.Comment.GetId(), data);
  }
  function replyOf(text) {
    var reply = new AscCommon.CCommentData(), now = new Date();
    reply.SetText(text); reply.SetUserName("Agent"); reply.m_sUserId = "agent:probe"; reply.m_sProviderId = "Teamlab";
    reply.m_sTime = String(now.getTime() - now.getTimezoneOffset() * 60000); reply.m_sOOTime = String(now.getTime());
    return reply;
  }
  var thread = String(parseInt(scope.guid, 16));
  changeThread(thread, function (data) { data.Add_Reply(replyOf("Done: east added.")); });
  changeThread(thread, function (data) { data.SetSolved(true); });
  var c = doc.GetCommentById(thread);
  return { replies: c.GetRepliesCount(), solved: c.IsSolved(), turnOff: AscCommon.g_oTableId.m_bTurnOff };
} catch (e) { return { error: String(e && e.stack || e) }; } }
```

→ `{replies:3, solved:true, turnOff:false}`. The person's page read the same values (`last: "Done: east added."`), and
its popover showed the Agent reply and the resolved state.

**Lock behaviour:** an open reply box or edit box on the person's side does **not** lock the comment
(`Comment.Lock.Get_Type()` = 1, none), and the agent's change applied. Two side effects showed up:

- **A half-typed reply in the person's box was wiped** when the agent's reply arrived. The box re-rendered empty.
- An open edit box (text being edited) survived. The person's later OK kept the agent's replies and solved state
  (`text … pls`, `replies 3`, `solved true` on both pages).

### 5. Traps

- Look up with `parseInt(actionId, 16)`. Passing the hex string straight to `GetCommentById` looks up the wrong
  number.
- A builder exception returns `undefined` from the command, the same as a skipped command. The first list call
  returned `null` until it was wrapped in try/catch. A reply's `IsSolved` is the likely cause; it is not in the snippet
  above.

## Spreadsheet (xlsx)

### 1. Notify

UI: click B2, Collaboration → Add Comment, type, Add.

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"{F80AA885-C2F8-8A96-7622-6B76D3A85AB7}","actionLink":{"action":{"type":"comment","data":"{F80AA885-C2F8-8A96-7622-6B76D3A85AB7}"}},"message":"@agent@agents.surogate.ai is Q1 North right?"}
```

Reply:

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"{F80AA885-C2F8-8A96-7622-6B76D3A85AB7}","actionLink":{"action":{"type":"comment","data":"{F80AA885-C2F8-8A96-7622-6B76D3A85AB7}"}},"message":"draft @agent@agents.surogate.ai check Q2 too!"}
```

A comment on the second sheet: `"actionId":"{6EE452C6-CC52-D9B4-B2E0-16E1F78AA4AC}"`, same shape.

### 2. List

Iterating the sheets gives each comment its sheet and cell. Ran as is (late join):

```js
function () { return Api.GetSheets().map(function (s) { return s.GetComments().map(function (c) { return { sheet: s.GetName(), cell: s.GetRangeByNumber(c.Comment.nRow, c.Comment.nCol).GetAddress(false, false, "xlA1", false), guid: c.Comment.asc_getGuid(), text: c.GetText(), match: c.Comment.asc_getGuid() === scope.guid }; }); }); }
```

→ `[[{sheet:"Sales", cell:"B2", guid:"{54E4B213-…}", text:"@agent@… late join cell", match:true}], []]`. Author, user
id, replies and solved state come from `c.GetAuthorName()`, `c.GetUserId()`,
`c.GetRepliesCount()`/`c.GetReply(i).GetAuthorName()/GetUserId()/GetText()` and `c.IsSolved()`. `c.GetQuoteText()`
returns the cell **value** ("120"), not its address. `c.GetId()` is `"sheet7_bca4c7b9-…"`. `Api.GetAllComments()`
covers workbook-level plus all sheets but gives no sheet. `Api.GetCommentById` only searches the **active** sheet and
workbook comments.

### 3. GUID

`c.Comment.asc_getGuid()` (internal field `sGuid`) equals the notify `actionId`, both live and on late join. The builder
`ApiComment` holds a **clone** of the comment (`this.Comment = comment.clone()`), and the clone keeps `sGuid`.

### 4. Reply and resolve

Ran as is (the comment's sheet is activated first; see the traps):

```js
function () { try {
  var sheets = Api.GetSheets(), sheet = null, c = null;
  for (var i = 0; i < sheets.length && !c; i++) {
    c = sheets[i].GetComments().filter(function (x) { return x.Comment.asc_getGuid() === scope.guid; })[0] || null;
    if (c) sheet = sheets[i];
  }
  if (!c) return { error: "not found" };
  sheet.SetActive();
  c.AddReply("Reply after activating Notes.", "Agent", "agent:probe");
  c.SetSolved(true);
  return { sheet: sheet.GetName(), cell: sheet.GetRangeByNumber(c.Comment.nRow, c.Comment.nCol).GetAddress(false, false, "xlA1", false), active: Api.GetActiveSheet().GetName(), turnOff: AscCommon.g_oTableId.m_bTurnOff };
} catch (e) { return { error: String(e && e.stack || e) }; } }
```

→ both pages: `Notes: … / solved=true / ["Reply after activating Notes."]`. `AddReply` sets no time, so the person saw
the reply with no date. `var r = c.GetReply(c.GetRepliesCount() - 1); r.SetTimeUTC(Date.now());` gives it one (seen as
"9:00 AM").

**Lock behaviour:** an open reply box or edit box on the person's side did not block the agent. The person's **draft
was kept**, but the **Reply button went disabled** until they typed another character. The person's OK on a stale edit
box kept the agent's replies and solved state.

### 5. Traps

- **Non-active sheet = silent no-op.** `ApiComment.AddReply/SetSolved` call `asc_changeComment`, which uses the
  **active** sheet's commentator. With the agent on "Sales" and the comment on "Notes", the call returned normally and
  the clone showed `replies:1`, but neither page changed. Call `sheet.SetActive()` first.
- **Changes apply asynchronously and the last clone wins.** `changeComment` waits for the comment-lock answer before
  applying. In the same command, a fresh `Api.GetAllComments()` still showed `replies 0` after `AddReply`.
  `find().AddReply(…)` followed by `find().SetSolved(true)` (two clones) ended with **solved=true and the reply lost**:
  the second change overwrote the replies array with its own stale clone. Do every change of one command on **one**
  `ApiComment` object, and between commands poll a fresh read until the change is there. That took about 50 ms here
  (one poll).
- Clicking the cell where the comment sits shows the popover in the person's view. One thread per cell.
- Not run: a reply or resolve inside a group of actions (`StartAction ["GroupActions"]`). Every call above ran outside
  one.

## Presentation (pptx)

### 1. Notify

UI: slide 2 thumbnail, Collaboration → Add Comment, type, Add.

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"{9D24FA3F-798C-22EB-4B89-84EF55C02C52}","actionLink":{"action":{"type":"comment","data":"{9D24FA3F-798C-22EB-4B89-84EF55C02C52}"}},"message":"@agent@agents.surogate.ai which month for Lisbon?"}
```

Reply:

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"{9D24FA3F-798C-22EB-4B89-84EF55C02C52}","actionLink":{"action":{"type":"comment","data":"{9D24FA3F-798C-22EB-4B89-84EF55C02C52}"}},"message":"@agent@agents.surogate.ai and the budget? thx"}
```

### 2. List

Ran as is (late join):

```js
function () { return Api.GetPresentation().GetAllComments().map(function (c) { var d = c.Comment.GetData(); return { slide: c.Comment.Parent && c.Comment.Parent.slide ? c.Comment.Parent.slide.num + 1 : null, guid: d.m_sGuid, text: c.GetText(), match: d.m_sGuid === scope.guid }; }); }
```

→ `[{slide:2, guid:"{7E147954-…}", text:"@agent@… late join slide", match:true}]`. These also work: `GetAuthorName`,
`GetUserId`, `GetText`, `IsSolved`, `GetRepliesCount`, `GetReply(i).GetAuthorName/GetUserId/GetText`, and
`GetTimeUTC`. `GetQuoteText()` is `null` (the anchor is a slide plus `Comment.x/y`). The slide is internal:
`Comment.Parent` is the slide's comments holder, and `.slide.num` is 0-based. `ApiSlide` has only `AddComment`, and
`ApiPresentation` only `GetAllComments`.

### 3. GUID

`c.Comment.GetData().m_sGuid` (internal) equals the notify `actionId` (web-apps
`asc_CCommentDataSlide.asc_getGuid = m_sGuid`), both live and on late join. The internal id is `c.Comment.Id`
(`"1_1"`).

### 4. Reply and resolve

**(a) Builder.** Works, but changes the data in place, as in Word, so a cancelled group cannot take it back:

```js
var c = Api.GetPresentation().GetAllComments().filter(function (x) { return x.Comment.GetData().m_sGuid === scope.guid; })[0];
c.AddReply("Checking the plan.", "Agent", "agent:probe");   // later, separately: c.SetSolved(true);
```

→ both pages `["Agent/agent:probe: Checking the plan."]`, then `solved:true`. The comment was on slide 2 while the
agent was on slide 1, and that worked.

**(b) Copy path.** The old data stays untouched, so a cancelled group can take it back. Ran as is:

```js
function () { try {
  function copy(d) {
    var out = new AscCommon.CCommentData();
    for (var k in d) if (Object.prototype.hasOwnProperty.call(d, k) && typeof d[k] !== "function") out[k] = d[k];
    out.m_aReplies = (d.m_aReplies || []).map(copy);
    return out;
  }
  var pres = Api.GetPresentation();
  var c = pres.GetAllComments().filter(function (x) { return x.Comment.GetData().m_sGuid === scope.guid; })[0];
  var data = copy(c.Comment.GetData());
  var reply = new AscCommon.CCommentData(), now = new Date();
  reply.SetText("Copy-path reply."); reply.SetUserName("Agent"); reply.m_sUserId = "agent:probe";
  reply.m_sTime = String(now.getTime() - now.getTimezoneOffset() * 60000); reply.m_sOOTime = String(now.getTime());
  data.Add_Reply(reply); data.SetSolved(true);
  var before = c.Comment.GetData();
  pres.Presentation.EditComment(c.Comment.Get_Id(), data);
  return { replacedData: c.Comment.GetData() !== before, oldUntouched: before.m_aReplies.length, replies: c.GetRepliesCount(), solved: c.IsSolved(), guid: c.Comment.GetData().m_sGuid, turnOff: AscCommon.g_oTableId.m_bTurnOff };
} catch (e) { return { error: String(e && e.stack || e), turnOff: AscCommon.g_oTableId.m_bTurnOff }; } }
```

→ `{replacedData:true, oldUntouched:0, replies:1, solved:true, guid kept}`. The person's popover showed "Agent …
Copy-path reply." with the green resolved tick. A slide `CCommentData` has **no `Copy()`**: it threw `TypeError … Copy
is not a function` and nothing changed. `asc_CCommentDataSlide` cannot be reached inside a command because the
connector's `var Asc = {}` shadows it, which is why the fields are copied by hand. `CPresentation.EditComment` checks
`Document_Is_SelectionLocked(changestype_MoveComment, …)` and quietly does nothing when the comment is locked.

**Lock behaviour:** an open reply box does not lock (`Lock.Get_Type()` = 1). After the agent resolved, the person's
draft was kept but Reply was disabled until they typed again. **When the person had the edit box open and the agent
replied (thread already resolved), the person's popover closed and their edit was lost.**

### 5. Traps

- **`ApiComment.GetId()` throws in the presentation** (`TypeError: this.Comment.GetDurableId is not a function`). The
  presentation uses Word's `ApiComment`. Use `c.Comment.Id` and the GUID instead.

## PDF

The PDF editor loads `sdkjs/word/sdk-all*.js` (there is no `sdkjs/pdf/sdk-all.js`). Comments are **annotations**. In
"Commenting" mode, Collaboration → Add Comment over a selected word created a **sticky note** (`textAnnot`) beside the
selection. The quoted words are not kept.

### 1. Notify

UI: double-click "March", Collaboration → Add Comment, type, Add.

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"5885824a","actionLink":{"action":{"type":"comment","data":"5885824a"}},"message":"@agent@agents.surogate.ai is March the right start?"}
```

Reply on the **same** thread:

```json
{"emails":["agent@agents.surogate.ai"],"actionId":"3a2cb2b7","actionLink":{"action":{"type":"comment","data":"3a2cb2b7"}},"message":"@agent@agents.surogate.ai and the fee terms? thx"}
```

### 2. List

Assembled from the calls that ran (`GetAllAnnots`, `GetAuthorName`, `GetContents`, `GetRect`, `GetReplies`,
`Annot.GetId/GetUserId/GetState`):

```js
function () { var out = []; var doc = Api.GetDocument(); for (var p = 0; p < doc.GetPagesCount(); p++) doc.GetPage(p).GetAllAnnots().forEach(function (a) { out.push({ page: p + 1, id: a.Annot.GetId(), author: a.GetAuthorName(), userId: a.Annot.GetUserId(), text: a.GetContents(), rect: a.GetRect(), state: a.Annot.GetState(), replies: a.GetReplies().map(function (x) { return x.GetAuthorName() + "/" + x.Annot.GetUserId() + ": " + x.GetContents(); }) }); }); return out; }
```

→ `{page:1, id:"1_1", author:"Flavius", text:"@agent@… is March the right start?", rect:[336.75,107.25,356.75,127.25],
state:2 after resolve, replies:["Agent/agent:probe: Checking the contract dates.", "Flavius: @agent@… and the fee terms?
thx"]}`. Solved means `Annot.GetState()` is 2 (Accepted, which is what the UI sets) or 5 (Completed). The builder has
no solved getter. The builder's annotation calls include `AddReply`, `GetReplies`, `Get/SetAuthorName`,
`Get/SetContents`, `GetModDate`, `GetCreationDate`, `GetUniqueName` and `GetRect`; there is no state or solved call.
The format of `GetCreationDate()`'s answer was not read.

### 3. GUID: not reachable

`GetAscCommentData()` makes `new asc_CCommentDataWord(null)` on every call, and that constructor sets
`m_nDurableId = AscCommon.CreateDurableId()` (random). Evidence: the notify said `5885824a`, the person's UI model said
`3a2cb2b7`, the reply's notify said `3a2cb2b7`, and two calls in a row on the agent's page gave `1a7c88d9` and
`09018799`. The stable shared id is the internal annotation id (`a.Annot.GetId()` = `"1_1"` in both pages, also the UI
model's `uid`), but the notify does not carry it.

The way in: match `notify.message` exactly against an annotation's `GetContents()` for a comment, or against a reply's
`GetContents()` (giving its parent annotation) for a reply. Filter on the notifying person (`Annot.GetUserId()` or
`GetAuthorName()`) and take the newest (`GetModDate()`/`GetCreationDate()`). This is reliable unless the same person
posts identical text twice. Edits fire no notify, so the text cannot have changed in between. A late-join run for PDF
was not done; the internal id is shared through co-editing.

### 4. Reply and resolve

Reply (builder). Ran as is:

```js
function () { try {
  var doc = Api.GetDocument(), target = null, page = null;
  for (var p = 0; p < doc.GetPagesCount() && !target; p++) doc.GetPage(p).GetAllAnnots().forEach(function (a) { if (!target && a.GetContents() === scope.text) { target = a; page = p + 1; } });
  if (!target) return { error: "not found" };
  var reply = Api.CreateTextAnnot(target.GetRect());
  reply.SetContents("Checking the contract dates.");
  reply.SetAuthorName("Agent");
  reply.Annot.SetUserId("agent:probe");
  var ok = target.AddReply(reply);
  return { ok: ok, page: page, replies: target.GetReplies().map(function (x) { return x.GetAuthorName() + ": " + x.GetContents(); }), turnOff: AscCommon.g_oTableId.m_bTurnOff };
} catch (e) { return { error: String(e && e.stack || e), turnOff: AscCommon.g_oTableId.m_bTurnOff }; } }
```

→ `{ok:true, page:1, replies:["Agent: Checking the contract dates."]}`. The person's popover showed "Agent 9:07 AM,
Checking the contract dates." (`CreateTextAnnot` sets the dates). The reply is not a separate annotation on the page.

Resolve (internal, the UI's own path). Ran as is:

```js
function () { try {
  var doc = Api.GetDocument(), target = null;
  for (var p = 0; p < doc.GetPagesCount() && !target; p++) doc.GetPage(p).GetAllAnnots().forEach(function (a) { if (!target && a.GetContents() === scope.text) target = a; });
  if (!target) return { error: "not found" };
  var annot = target.Annot, data = new AscCommon.CCommentData();
  data.Read_FromAscCommentData(annot.GetAscCommentData());
  data.SetSolved(true);
  doc.Document.EditComment(annot.GetId(), data);
  return { state: annot.GetState(), solved: annot.GetAscCommentData().asc_getSolved(), replies: target.GetReplies().length, turnOff: AscCommon.g_oTableId.m_bTurnOff };
} catch (e) { return { error: String(e && e.stack || e), turnOff: AscCommon.g_oTableId.m_bTurnOff }; } }
```

→ `{state:2, solved:true, replies:1}`, with the same values on the person's page and a green tick in their popover.

**Lock behaviour:** an open reply box kept its draft, but Reply was disabled until the person typed again. With an open
edit box, the agent's reply showed up underneath and the person's draft edit survived. Their OK kept all three replies
and `solved:true`.

### 5. Traps

- The `actionId` is useless in PDF (section 3), and it **differs between the comment's notify and a reply's notify on
  the same thread**.
- `CAnnotationBase.EditCommentData` runs inside `History.StartNoHistoryMode()…EndNoHistoryMode()`. A throw in there
  would leave history off (the `m_bTurnOff` poison). It also **deletes replies missing from the data passed in**
  (`aReplyToDel`), so always build the data from the annotation's current `GetAscCommentData()`.
- `ApiBaseAnnotation.AddReply` throws (via `AscBuilder.throwException`) if the argument is not an
  `ApiTextAnnotation`. It throws before changing anything (`turnOff` false).
- The person's replies arrive on the agent's side with ids like `off_20` (made in no-history mode). They display and
  list normally.

---

## What this means for the plan

1. **Notify handling (browser → runtime):** pass along `{actionLink.action.data, message, emails}` and the document.
   Treat the id as the **thread** key. A mention in a reply carries the parent's id, and `message` is the full text of
   the comment or reply that mentioned the agent.
2. **Thread lookup per editor (agent page):**
   - word: `doc.GetCommentById(String(parseInt(actionId, 16)))`. Its `GetId()` is the durable id in decimal.
   - cell: loop `Api.GetSheets()` → `s.GetComments()` → `c.Comment.asc_getGuid() === actionId`. Keep `s` for the sheet
     name, and `c.Comment.nRow/nCol` for the cell.
   - slide: `Api.GetPresentation().GetAllComments()` → `c.Comment.GetData().m_sGuid === actionId`. The slide is
     `c.Comment.Parent.slide.num + 1`.
   - pdf: ignore `actionId`. Loop the pages → `GetAllAnnots()`, match `message` against `GetContents()` of the
     annotation or any reply, filter on the person's user id, take the newest, and key the thread by
     `a.Annot.GetId()`.
3. **Reply and resolve:**
   - word: the existing `changeThread`/`replyOf`.
   - cell: one `ApiComment` with `sheet.SetActive()` first, then `AddReply(text, name, id)`,
     `GetReply(last).SetTimeUTC(now)` and `SetSolved(true)`; poll a fresh read until it has landed.
   - slide: the copy-then-`Presentation.EditComment` path (cancellable).
   - pdf: `CreateTextAnnot` + `SetContents`/`SetAuthorName`/`Annot.SetUserId` + `AddReply`; resolve with
     `CCommentData.Read_FromAscCommentData(annot.GetAscCommentData())`, `SetSolved(true)`,
     `doc.Document.EditComment(id, data)`.
4. **Order:** acknowledge → reply → resolve last. Resolving can close or hide the person's open popover (slides), and
   every incoming change can disturb a draft (Word wipes it; cell, slide and PDF disable Reply until the person types).
5. **Guards to keep:** try/catch every command (an exception looks like a skipped command), check `poisoned()` after a
   failure, and never call `ApiComment.GetId()` in a presentation.

## Not covered

- Comments made while the agent's page was closed and the document was saved and reopened under a new key. Word's
  durable id and the spreadsheet's `sGuid` are written to the file, which suggests they survive; this was not run.
- Workbook-level and presentation-level comments (`bDocument`).
- A PDF late join.
- Spreadsheet comment changes inside a group of actions.
- The format of a PDF annotation's `GetCreationDate()`.
