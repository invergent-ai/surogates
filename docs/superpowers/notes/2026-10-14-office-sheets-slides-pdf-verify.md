# Office editors: verify results for sheets, slides and PDF

Run on 2026-10-14 against the DEV document server (Euro-Office 9.3.5, `https://office.densemax.local`, sdkjs path
`/9.3.5-dev001/`), with runtime-office's own page code (`PagePool`, the host page, the connector) driving an agent editor
and a person's editor on one document key. It answers the spec's verify item 12 and everything plan 4 needed to know
about the spreadsheet, slide and PDF editors' builder APIs. The spike's scripts were throwaway and are not kept; every
working snippet is copied below, with what came back.

## Summary

| Question | Answer |
|---|---|
| Verify item 12: PDF text edited through `RecognizeContent` keeps its embedded fonts? | Kept for every glyph the embedded subset has; a missing glyph comes from a new embedded Arial subset, mid-word. But recognition rewrites the whole page as text boxes (lines merge and re-wrap, browser-made PDFs come back a point smaller and letter-spaced), so PDF text `replace` is left out of plan 4 (Flavius, 2026-10-14). |
| Spreadsheets: all or nothing? | `GroupActions` holds every in-command edit; sheet add/delete/move land asynchronously when a co-editor is connected and must land before the group ends. |
| Spreadsheets: two writers? | Builder writes take no lock and diverge; the agent takes the editor's own range locks (`_isLockedCells`) inside the group first. |
| Slides: ids? | `slide.Slide.Get_Id()` (internal), the same in every editor; no locks in fast co-editing: guard replace/remove with a content hash. |
| Slides: text fidelity? | The editor lays text out as Chromium does to 0.01 px with the document server's own font files and `font-kerning:none; font-variant-ligatures:none; text-rendering:geometricPrecision; line-height:1.2`. |
| Slides: a throw inside a builder call? | It can leave the editor's object registry off (`AscCommon.g_oTableId.m_bTurnOff`): the agent's next change drops the person into view mode and the document never saves. Restore the registry in the script's catch, cancel the group, and recycle a page left broken. |
| Slides: Claude Slides' format natively? | Fills (solid, alpha, linear gradients), preset and custom geometry, strokes (dash, caps, arrow heads), connectors as custom paths, unblurred outer shadows, rotation and flips, run styling, picture crops, tables, hidden slides, fade/push/Morph transitions and builds are native. Radial gradients, blurred or inset shadows, filters and blend modes match only as pictures Chromium draws. |
| PDF: all or nothing? | `StartAction ["GroupActions"]` wedges the PDF editor: one command per batch, and an `Undo` straight after a failed one. |
| Flush, every format | The host page's `onDocumentStateChange(false)` after the last send. The modified flag read in a command is wrong for spreadsheets (always true) and slides (false before the send). |

## Spreadsheets

Run 2026-10-14 against `https://office.densemax.local` (sdkjs path `/9.3.5-dev001/`), probe port 8792, with
runtime-office's own page code (`PagePool`, host page, connector). Agent editor `agent:spike`, person editor
`user:spike` ("Flavius") on the same random key. Scripts are in `cell/`. Reads of the editor's own source
were done on `https://office.densemax.local/sdkjs/cell/sdk-all.js` (builder API, readable) and `sdk-all-min.js`
(spreadsheet_api, minified), saved in `cell-src/` with `src.py`/`msrc.py` to print a function: those
reads explain behaviour, the commands below prove it.

### Summary

| Q | Answer in one line |
|---|---|
| 1 | One `range.SetValue(rows)` (2-D) writes text/numbers/booleans/formulas, formulas live; strings parse as typed (`"0042"`→42, `"'0042"` text, ISO date→date); `null`→`#N/A`, use `""`; 2,000 cells ~50 ms. |
| 2 | Outline in one command (2 ms); merges, chart range/anchor, protection via internals; compact view ≈ 9–11 chars/cell (≈3,000 cells per 30k chars); search = bulk `GetText()` scan of used ranges (`Find` is wrong). |
| 3 | `expect` = the view's `GetText()` grid; compare all guards before any write; 2,000 cells in ~2 ms. |
| 4 | Setters return nothing; `Api.HexColor` broken (use `CreateColorFromRGB`); align strings `left/center/right/justify`, `top/center/bottom/distributed/justify`; width in Excel chars; number formats unvalidated. |
| 5 | `AddChart(range, inRows, type, style, wEmu, hEmu, col0, colOffEmu, row0, rowOffEmu)`; `bar`=columns, `horizontalBar`, `lineNormal`, `pie`, `doughnut`, `area`, `scatter` (`column`/`line` throw); no public id/delete: `SetName` handle (survives in the file) + internal `Chart.deleteDrawingBase()`. |
| 6 | AddSheet goes after the agent's active sheet; rename is in-command; **add/delete/move are async with a co-editor** (lock round trip); last visible sheet delete silently refused; person's active sheet untouched. |
| 7 | GroupActions cancel/commit works for all in-command edits in both editors; **async sheet ops escape a group ended at once** → keep the group open until they land. |
| 8 | Host page `onDocumentStateChange(false)` is the flush signal (11–18 ms); forcesave after it → status 6 with the edit; the in-command modified flag is always true (pre-check must read it from the frame). |
| 9 | **Builder writes take no lock: concurrent writes to a cell diverge permanently** (person's editor shows a value not in the document); take range locks with internal `_isLockedCells` inside the group first; refused = person editing. |
| 10 | Detect with `ws.getSheetProtection()`, `ws.sheetProtection.asc_get*()`, `ws.isIntersectLockedRanges`, `api.asc_isProtectedWorkbook()`; builder bypasses column width, AddSheet and SetName protection: refuse up front. |

Every internal name used (`range.range.bbox`, `worksheet`, `workbook.oApi`, `mergeManager`, `Chart.*`,
`collaborativeEditing`, `_isLockedCells`, `_participants`, `sheetProtection`) is reachable from a command through
`Api.GetSheets()[i].worksheet…`; the bare name `Asc` is NOT (runtime-office's command prelude `var Asc = {}`
shadows it), so constants come from `AscCommon`/`AscCommonExcel`.

**Correction to "already found".** `range.SetValue(2-D array)` does NOT drop formulas. The formula is set, but its
value is computed only after the command returns, and a multi-cell `GetFormula()` returns values, so a read in the
same command shows an empty value. Per-cell reads in the next command show every formula live (Q1).

---

### Q1. Writing values and formulas

**Answer:** one `range.SetValue(rows)` with a 2-D array writes text, numbers, booleans, `""` and formulas in one go,
every formula live; each string is parsed exactly as if typed into the cell (so `"0042"` becomes the number 42, a
leading `'` keeps text); `null`/`undefined` become `#N/A`; `""` clears. 2,000 cells take ~50 ms in the editor
(~0.4 s round trip), 10,000 cells 214 ms (~0.9 s).

#### Code: mixed 2-D block (`cell/q1a.js`), read per cell in the next command (`cell/read.js`)

```js
function () {
  try {
    var sheet = Api.GetSheet("Sales");
    var r = sheet.GetRange("A6:F7");
    var ret = r.SetValue([["West", 50, 60, "=B6+C6", true, ""], ["Sum", "=SUM(B2:B6)", "=SUM(C2:C6)", "=SUM(D2:D6)", false, "x"]]);
    return { ret: ret };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
// -> {"ret": true}
```

```js
// read.js: per cell [GetValue, GetFormula, GetText, GetValue2, GetNumberFormat]
function () {
  try {
    var sheet = Api.GetSheet(scope.sheet || "Sales"), rng = sheet.GetRange(scope.range), out = [];
    var rows = rng.GetRowsCount(), cols = rng.GetColumnsCount();
    for (var i = 1; i <= rows; i++) {
      var row = [];
      for (var j = 1; j <= cols; j++) {
        var c = rng.GetCells(i, j);
        row.push([c.GetValue(), c.GetFormula(), c.GetText(), c.GetValue2(), c.GetNumberFormat()]);
      }
      out.push(row);
    }
    return out;
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
Came back (A6:F7, next command):
```
[["West",…], ["50",…], ["60",…], ["110","= B6+C6","110","110","General"], ["TRUE",…], ["","","","","General"]]
[["Sum",…], ["330","= SUM(B2:B6)",…], ["375","= SUM(C2:C6)",…], ["705","= SUM(D2:D6)",…], ["FALSE",…], ["x",…]]
```
In the SAME command as the write: `C2.GetValue()` = `""`, `C2.GetFormula()` = `"= 1+1"`; next command `"2"`.

#### Typing: each input written in a 2-D column, then probed with `ISNUMBER/ISTEXT/ISLOGICAL` (`cell/q1b.js`)

| input | GetValue | GetText | number format set | type |
|---|---|---|---|---|
| `"0042"` | `42` | `42` | General | number |
| `12.5` / `"12.5"` / `" 7 "` / `"-3"` / `"(4)"` | `12.5` / `12.5` / `7` / `-3` / `-4` | same | General | number |
| `"2026-01-05"`, `"1/5/2026"` | `46027` | `1/5/2026` | `m/d/yyyy` | number (date serial) |
| `"05.01.2026"` | `05.01.2026` | same | General | text (en locale) |
| `"50%"` | `0.5` | `50%` | `0%` | number |
| `"1,200"` / `"€1,200"` | `1200` | `1,200` / `€1,200` | `#,##0` / `\€#,##0_);[Red](\€#,##0)` | number |
| `"1e3"` | `1000` | `1.00E+03` | `0.00E+00` | number |
| `"12:30"` | `0.5208…` | `12:30` | `h:mm` | number |
| `"'0042"` | `0042` | `0042` | General | **text** |
| `"TRUE"` / `true` | `TRUE` | `TRUE` | General | logical |
| `"=1/0"` | `#DIV/0!` | | | formula `= 1/0` |
| `null` | `#N/A` | | | error value written |

Clearing (`cell/q1c.js`): `SetValue("")`, `ClearContents()` and `Clear()` all leave a blank cell
(`ISBLANK` TRUE, `COUNTA` 0, `LEN` 0). **Only `SetValue("")` keeps the cell's formatting**: after
`SetBold(true)` + `SetNumberFormat("0.00")`, `SetValue("")` left `0.00`/bold, while `ClearContents()` (its source
calls `range.cleanAll()`, despite the name) and `Clear()` both reset to `General`/not bold. A short row in a 2-D
array (`[["a","b"],["c"]]` into `A1:C2`) wrote `#N/A` into the three missing cells.

#### Code: 2,000 cells (100 × 20; text, numbers, booleans, `""`, formulas) (`cell/q1d.js`)
```js
function () {
  try {
    var t0 = Date.now(), sheet = Api.GetSheet("Big"), rows = [];
    for (var i = 0; i < 100; i++) {
      var row = [];
      for (var j = 0; j < 20; j++) {
        var m = j % 5;
        row.push(m === 0 ? "Item " + i : m === 1 ? i * j : m === 2 ? true : m === 3 ? "" : "=B" + (i + 1) + "*2");
      }
      rows.push(row);
    }
    sheet.GetRange("A1:T100").SetValue(rows);
    return { ms: Date.now() - t0 };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
// -> {"ms": 49}   (pc.py round trip 0.43 s); next command: E100 = "198", formula "= B100*2", used range A1:T100
// 200 × 50 = 10,000 cells: {"ms": 214}, round trip 0.93 s
```

#### Gotchas
- The SDK's 2-D path (`ApiRange.SetValue`, read in `sdk-all.js`) loops cell by cell: `null == value` →
  `cErrorLocal.na`, then `cell.setValue(String(value))`, i.e. typed-input parsing. **Send `""` for an empty cell,
  never `null`**, and send text that must stay text (leading zeros, ids, `"=..."` meant literally, `"TRUE"` meant as
  text) with a leading `'`.
- A row shorter than the range writes `#N/A` into the missing cells (proved above): pad rows with `""`, or size the
  range from the array.
- A JS `Date` in a 2-D array becomes its `toString()` text; the agent sends ISO date strings, which become dates.
- Values of formulas are not computed until the command returns: never read a formula's value in the write command.
- `GetValue()` returns strings for numbers (`"120"`), and the multi-cell form returns a 2-D array of strings.
- `Api.AddSheet(name)` returns `undefined` (not the sheet): get it with `Api.GetSheet(name)` (Q6).

---

### Q2. Reading

**Answer:** everything for an outline is one cheap command (2 ms for 3 sheets): name, index, visibility, used range,
sheet protection, merged areas, and per chart its name, type, title, data range and anchor cell. Public API covers
name/index/visibility/used range/chart name/type/title; **merged areas, chart data range, chart anchor and sheet
protection need internal names** (`ws.mergeManager.getAll()`, `chart.Chart.getCommonRange()`,
`chart.Chart.drawingBase.from`, `ws.getSheetProtection()`). A view in a compact shape (`values` 2-D display text +
`formulas` map + non-General `formats` map) costs ~9–11 characters per cell: **30,000 characters ≈ 2,700–3,200
cells** of ordinary data (fewer when cells hold long text). Search: **scan each sheet's used range with one bulk
`GetText()`**; `Find`/`FindNext` is wrong and 10× slower.

#### Code: outline (`cell/q2outline.js`, run on `fixtures/cell-rich.xlsx`, built by `cell/make_cell_fixtures.py`)
```js
function () {
  function col(c) { var s = ""; for (c++; c > 0; c = Math.floor((c - 1) / 26)) s = String.fromCharCode(65 + (c - 1) % 26) + s; return s; }
  function a1(r, c) { return col(c) + (r + 1); }
  function box(b) { return b.r1 === b.r2 && b.c1 === b.c2 ? a1(b.r1, b.c1) : a1(b.r1, b.c1) + ":" + a1(b.r2, b.c2); }
  try {
    var t0 = Date.now(), sheets = Api.GetSheets(), api = sheets[0].worksheet.workbook.oApi, out = { sheets: [] };
    out.workbook_protected = !!api.asc_isProtectedWorkbook();
    out.active = Api.GetActiveSheet().GetName();
    for (var i = 0; i < sheets.length; i++) {
      var s = sheets[i], ws = s.worksheet, used = s.GetUsedRange(), ub = used.range.bbox;
      var info = { name: s.GetName(), index: s.GetIndex(), visible: s.GetVisible(), used: box(ub), protected: !!ws.getSheetProtection() };
      // the first two rows of the used range, as shown: the header is usually one of them
      var top = s.GetRange(a1(ub.r1, ub.c1) + ":" + a1(Math.min(ub.r1 + 1, ub.r2), Math.min(ub.c2, ub.c1 + 29))).GetText();
      info.top = Array.isArray(top) ? (Array.isArray(top[0]) ? top : [top]) : [[top]];
      info.merged = ws.mergeManager.getAll().slice(0, 50).map(function (m) { return box(m.bbox); });
      info.charts = s.GetAllCharts().map(function (c) {
        var from = c.Chart.drawingBase && c.Chart.drawingBase.from;
        return { name: c.GetName(), type: c.GetChartType(), title: String(c.GetTitle() || "").replace(/\s+$/, ""), range: c.Chart.getCommonRange(), anchor: from ? a1(from.row, from.col) : null };
      });
      out.sheets.push(info);
    }
    out.ms = Date.now() - t0;
    return out;
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
Came back:
```json
{"sheets": [
  {"name": "Data", "index": 0, "visible": true, "used": "A1:G7", "protected": false,
   "top": [["Regional sales 2026", "", "", "", "", "", ""], ["Region", "Q1", "Q2", "Total", "", "", ""]],
   "merged": ["A1:D1", "F3:G4"],
   "charts": [{"name": "Chart 1", "type": "bar", "title": "Sales by region", "range": "=Data!$A$2:$C$7", "anchor": "F8"}]},
  {"name": "Hidden", "index": 1, "visible": false, "used": "A1", "protected": false, "top": [["secret"]], "merged": [], "charts": []},
  {"name": "Locked", "index": 2, "visible": true, "used": "A1:B3", "protected": true, "top": [["Item", "Amount"], ["Rent", "1000"]], "merged": [], "charts": []}],
 "workbook_protected": false, "active": "Data", "ms": 2}
```
- Header row: there is no API for it. The first row of the used range is the header in the common case; here row 1
  is a merged title (`A1:D1` in `merged`) and row 2 the header. Returning the top two rows plus the merged list lets
  the model tell; a heuristic ("first row whose non-empty cells are all text, skipping a row that is one merged
  cell") is possible but not needed.
- An empty sheet's used range is `"A1"` with an empty `A1` (checked on a new sheet).
- A chart from `GetAllCharts()` (`ApiChart`) has **no `GetInternalId` and no `Delete`** in the spreadsheet editor
  (enumerated: `GetName/SetName`, `GetChartType`, `GetTitle/SetTitle`, `GetWidth/GetHeight` (EMU), `SetPosition`,
  `SetSize`, series setters, …). Its series objects expose only `GetChartType/ChangeChartType`. Its internal
  `Chart.Id` (`"13"`) exists. Chart identity and removal: see Q5.

#### Code: view (`cell/q2view2.js`)
```js
function () {
  function col(c) { var s = ""; for (c++; c > 0; c = Math.floor((c - 1) / 26)) s = String.fromCharCode(65 + (c - 1) % 26) + s; return s; }
  try {
    var t0 = Date.now(), sheet = Api.GetSheet(scope.sheet), rng = sheet.GetRange(scope.range);
    var rows = rng.GetRowsCount(), cols = rng.GetColumnsCount(), box = rng.range.bbox;
    var texts = rng.GetText(), formulas = {}, formats = {};
    if (rows === 1 && cols === 1) texts = [[texts]];
    for (var i = 0; i < rows; i++) for (var j = 0; j < cols; j++) {
      var c = sheet.GetRangeByNumber(box.r1 + i, box.c1 + j), f = c.GetFormula(), fmt = c.GetNumberFormat(), a = col(box.c1 + j) + (box.r1 + i + 1);
      if (typeof f === "string" && f.charAt(0) === "=") formulas[a] = "=" + f.slice(2);
      if (fmt !== "General") formats[a] = fmt;
    }
    var view = { range: scope.range, values: texts, formulas: formulas, formats: formats };
    var json = JSON.stringify(view);
    return { ms: Date.now() - t0, cells: rows * cols, chars: json.length, charsPerCell: Math.round(json.length / (rows * cols) * 10) / 10, head: json.slice(0, 300) };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
| range | cells | ms in editor | chars | chars/cell |
|---|---|---|---|---|
| Big!A1:T100 (text, numbers, booleans, blanks, formulas) | 2,000 | 3 | 18,630 | 9.3 |
| Big!A101:AX300 (numbers, 1 formula in 4) | 10,000 | 26 | 107,687 | 10.8 |
| Sales!A1:F7 | 42 | 1 | 393 | 9.4 |

The same data as one object per cell (`{"v":…,"f":…,"n":…}`) costs 15–16 chars/cell (`cell/q2view.js`): about
2,000 cells per 30,000 characters. Head of the compact form:
`{"range":"A1:F7","values":[["Region","Q1","Q2","Total","",""],["North","120","140","260","",""],…],"formulas":{"D2":"=B2+C2",…},"formats":{}}`.
- Reading is fast: per-cell `GetFormula` + `GetNumberFormat` over 10,000 cells is ~26 ms; the budget, not speed, is
  the limit. Stop at a row when the running size passes the budget and return `next` (as `word.js` does for tables).
- `GetText()` is what the person sees (`1/5/2026`, `50%`, `€1,200`); `GetValue()` is the raw value as a string
  (`46027`, `0.5`, `1200`). `GetFormula()` on one cell gives `"= B6+C6"`: strip the space (`"=" + f.slice(2)`).
  On a multi-cell range `GetFormula()` is the first cell's formula or `GetValue2()` (source), so formulas are read
  per cell. Single-cell `GetText()`/`GetValue()` return a string, not a 2-D array: wrap it.

#### Code: search (`cell/q2search.js`), scan vs `Find`/`FindNext`, over 5 sheets (~15,000 used cells)
```js
// scan: one bulk GetText per sheet
var used = sheets[s].GetUsedRange(), box = used.range.bbox, texts = used.GetText();
if (!Array.isArray(texts)) texts = [[texts]];
for (var i = 0; i < texts.length; i++) for (var j = 0; j < texts[i].length; j++)
  if (String(texts[i][j]).toLowerCase().indexOf(q) >= 0) matches.push([sheets[s].GetName(), col(box.c1 + j) + (box.r1 + i + 1), texts[i][j]]);
// Find/FindNext
var u = sheets[s].GetUsedRange(), first = u.Find({ What: scope.query, LookIn: "xlValues", LookAt: "xlPart", SearchOrder: "xlByRows", SearchDirection: "xlNext", MatchCase: false });
var cur = first; while (cur) { found.push(...); cur = u.FindNext(cur); if (cur && cur.GetAddress(...) === first.GetAddress(...)) break; }
```
Came back, query `"item 1"`: scan `scanTotal: 44` in `scanMs: 22` (Big!A2, F2, K2, P2, A11, …); Find `findN: 23` in
`findMs: 222`, starting with `["Sales","A2"]` (that cell is "North": a false match) and missing every match outside
column A. Query `"West"`: scan found `Sales!A6`; **Find found nothing**.
- `Find` drives the editor's UI search engine (`wb.Search(options)`, source) with options that do not map cleanly;
  do not use it. The scan matches on displayed text; add formula text if the agent should find references.

---

### Q3. The guard

**Answer:** `expect` should hold the range **as displayed (`GetText()`)**, which is exactly the `values` grid the
view returns, so the agent copies it unchanged. Compare it cell by cell as strings against one bulk `GetText()` of
the range, **all guards of a batch first, before any write**; refuse with what the range shows now. Cost: 2,000
cells compared in ~2 ms (guard + 2,000-cell write 13 ms).

#### Code (`cell/q3guard.js`)
```js
function () {
  function grid(v) { return Array.isArray(v) ? (Array.isArray(v[0]) ? v : [v]) : [[v]]; }
  // null when the range shows exactly `expect`, else what it shows now
  function differs(range, expect) {
    var now = grid(range.GetText());
    if (now.length !== expect.length) return now;
    for (var i = 0; i < now.length; i++) {
      if (now[i].length !== expect[i].length) return now;
      for (var j = 0; j < now[i].length; j++) if (String(now[i][j]) !== String(expect[i][j])) return now;
    }
    return null;
  }
  try {
    var t0 = Date.now(), ops = scope.ops;
    // every guard first, against the workbook as it was before this batch
    for (var k = 0; k < ops.length; k++) {
      if (!ops[k].expect) continue;
      var now = differs(Api.GetSheet(ops[k].sheet).GetRange(ops[k].range), ops[k].expect);
      if (now) return { refused: { op: k, reason: "changed since you read it", now: now }, ms: Date.now() - t0 };
    }
    for (k = 0; k < ops.length; k++) Api.GetSheet(ops[k].sheet).GetRange(ops[k].range).SetValue(ops[k].values);
    return { applied: ops.length, ms: Date.now() - t0 };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
- Scope `{"ops":[{"sheet":"Sales","range":"A2:D3","expect":[["North","120","140","260"],["East","90","110","200"]],"values":[["North",121,141,"=B2+C2"],["East",91,111,"=B3+C3"]]}]}`
  → `{"applied": 1, "ms": 2}`.
- The same scope again (now stale) → `{"refused": {"op": 0, "reason": "changed since you read it", "now": [["North","121","141","262"],["East","91","111","202"]]}, "ms": 0}`.
- Big!A1:T100 (2,000 cells), expect = its current `GetText()`: pass + write `ms: 13`; one cell differing: refused, `ms: 2`.

#### Why `GetText` and not the others
- `GetText` is what the view shows and what the person sees; the model copies it verbatim. It does not depend on
  column width (a column 2 wide still gave `"1,234,567.89"`, no `###`).
- `GetValue` would make the agent carry a second grid (`46027` for a date, `0.5` for `50%`) it never reads.
- Formulas (or "formula else value") are stable against recalculation, but a formula cell whose result changed
  because someone edited a precedent elsewhere has really changed what the range shows; refusing with the new
  values is right, and the refusal carries them, so the retry costs no extra read.
- Gotcha: a person changing only a number format (e.g. `0.00`) changes `GetText` and trips the guard: acceptable,
  the refusal shows the new text.

#### Gotchas
- Check every guard before any write: a formula written or a precedent changed earlier in the same command is not
  recalculated until the command returns (Q1), so a later op's `GetText` would see `""` or the old result.
  (`Api.RecalculateAllFormulas()` does compute in-command, 74 ms here, but it re-parses every formula in the
  workbook: a test utility, not something to run per edit.)
- Never write `GetText` values back as `values`: formulas become constants and text such as `0042` turns numeric.

---

### Q4. Formatting

**Answer:** all setters return `undefined` (alignment returns `true`/`false`), so read back to verify. Number format:
any Excel format code string, **not validated** (garbage is stored). Bold/italic/wrap: coerced with `!!`. Colours:
**`Api.CreateColorFromRGB(r, g, b)` only; `Api.HexColor` is broken in the spreadsheet editor** (it builds the
document/slide `ApiColor` `{type, value}`, the cell setters read `.color`, so `SetFillColor(Api.HexColor(...))`
silently removed the fill): parse `#RRGGBB` in the script. Horizontal: `left|center|right|justify`; vertical:
`top|center|bottom|distributed|justify`; anything else returns `false` and changes nothing. Column width is in
Excel character units (default `8.43`): `sheet.SetColumnWidth(index0, width)` for one column,
`range.SetColumnWidth(width)` for every column of the range.

#### Code (`cell/q4format.js`), then a read-back command
```js
function () {
  try {
    var s = Api.GetSheet("Notes"), r = s.GetRange("W1:X2"), out = {};
    r.SetValue([[1234.5, "text"], [0.25, "wrap me please, this is long"]]);
    function tryIt(name, f) { try { out[name] = f(); if (out[name] === undefined) out[name] = "undefined"; } catch (e) { out[name] = "THROW " + String(e).slice(0, 120); } }
    tryIt("numFmt", function () { return s.GetRange("W1").SetNumberFormat("#,##0.00 \"EUR\""); });
    tryIt("numFmtBad", function () { return s.GetRange("W2").SetNumberFormat("not a format ][["); });
    tryIt("bold", function () { return r.SetBold(true); });
    tryIt("boldStr", function () { return s.GetRange("X1").SetBold("false"); });
    tryIt("italic", function () { return r.SetItalic(true); });
    tryIt("hex", function () { var c = Api.HexColor("#FF8800"); return { type: typeof c.color, color: typeof c.color === "object" ? "obj" : c.color, keys: Object.keys(c) }; });
    tryIt("fillHex", function () { return s.GetRange("W1").SetFillColor(Api.HexColor("#FF8800")); });
    tryIt("fillRGB", function () { return s.GetRange("X1").SetFillColor(Api.CreateColorFromRGB(255, 136, 0)); });
    tryIt("fontColor", function () { return s.GetRange("X2").SetFontColor(Api.CreateColorFromRGB(200, 0, 0)); });
    ["left", "center", "right", "justify", "general", "centre"].forEach(function (a) { tryIt("h_" + a, function () { return s.GetRange("W1").SetAlignHorizontal(a); }); });
    ["top", "center", "bottom", "distributed", "justify", "middle"].forEach(function (a) { tryIt("v_" + a, function () { return s.GetRange("W1").SetAlignVertical(a); }); });
    s.GetRange("W1").SetAlignHorizontal("center"); s.GetRange("W1").SetAlignVertical("top");
    tryIt("wrap", function () { return s.GetRange("X2").SetWrap(true); });
    tryIt("colW_sheet", function () { return s.SetColumnWidth(22, 20); });          // column W
    tryIt("colW_range", function () { return s.GetRange("X1:Y1").SetColumnWidth(30); });
    return out;
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
Came back: every setter `"undefined"`; `hex: {"keys": ["type","value"]}` (no `.color`);
`h_left/center/right/justify: true, h_general: false, h_centre: false`;
`v_top/center/bottom/distributed/justify: true, v_middle: false`.

Read back (internal getters `range.range.getFont()`, `getFillColor()`, `getAlign()`; public `GetText`,
`GetNumberFormat`, `GetWrapText`, `GetColumnWidth`):
```json
{"W1": {"text": "1,234.50 EUR", "fmt": "#,##0.00 \"EUR\"", "bold": true, "italic": true, "fill": null, "h": 2, "v": 4},
 "X1": {"text": "text", "bold": true, "fill": "ff8800"},
 "W2": {"text": "not a for1at ]", "fmt": "not a format ][["},
 "X2": {"font": "c80000", "wrap": true},
 "widths": [8.43, 20, 30, 30]}
```
(`V1` untouched 8.43, `W` 20 from the sheet call, `X` and `Y` 30 from the range call; `W1` fill null: the
`HexColor` fill was dropped; `X1` `SetBold("false")` stayed bold.)

#### Gotchas
- Validate the number format in the runtime (or let the model see the `GetText()` it produces in the result): the
  editor stores any string, and the person then sees nonsense.
- Pass booleans, not strings, for bold/italic/wrap.
- Colours: `function rgb(hex) { var n = parseInt(hex.replace(/^#/, ""), 16); return Api.CreateColorFromRGB(n >> 16 & 255, n >> 8 & 255, n & 255); }`.
  `SetFillColor("No Fill")` removes a fill.
- Protection: every format setter checks `_checkProtection(formatCells)` and returns `null` doing nothing on a
  protected sheet, **except `SetColumnWidth` (sheet and range), which has no protection check** in the source
  (`worksheet.setColWidth` directly): guard it yourself (Q10).
- `GetFillColor()` returns `"No Fill"` or an object whose `RGB()` is missing (the same class mix-up): read colours
  through internals if ever needed; the outline/view do not need them.

---

### Q5. Charts

**Answer:** `sheet.AddChart(dataRange, inRows, type, styleIndex, widthEmu, heightEmu, fromCol, colOffsetEmu,
fromRow, rowOffsetEmu)`: sizes and offsets in **EMU** (9525 per px at 96 dpi, 36000 per mm), anchor column/row
**0-based** (anchor cell `I10` = col 8, row 9), anchored one-cell. Type strings are the builder's own
(`ChartBuilderTypeToInternal`): **`bar` is vertical columns, `horizontalBar` horizontal bars, `lineNormal`
(not `line`), `pie`, `doughnut`, `area`, `scatter`**, plus stacked/3-D/marker variants; `column` and `line`
**throw** (`TypeError: Cannot set properties of null (setting 'x')`) and add nothing. Title:
`chart.SetTitle(text, fontSizePt)`. **There is no public id and no public delete**: the agent finds its chart
again by a name it sets (`chart.SetName("…")`, propagates to the person), and removes it with the internal
`chart.Chart.deleteDrawingBase()`, which reached the person's editor.

#### Code: every candidate type (`cell/q5chart.js`, on `cell-rich.xlsx`)
```js
function () {
  try {
    var s = Api.GetSheet("Data"), out = {}, EMU_PER_PX = 9525;
    var types = ["bar", "column", "horizontalBar", "lineNormal", "line", "pie", "doughnut", "area", "scatter", "lineNormalMarker", "barStacked"];
    for (var i = 0; i < types.length; i++) {
      try {
        var c = s.AddChart("'Data'!$A$2:$C$7", false, types[i], 2, 480 * EMU_PER_PX, 288 * EMU_PER_PX, 8, 0, 9 + i * 16, 0);
        if (!c) { out[types[i]] = "null"; continue; }
        c.SetTitle("T " + types[i], 12);
        c.SetName("agent:chart:" + types[i]);
        var from = c.Chart.drawingBase.from;
        out[types[i]] = { type: c.GetChartType(), name: c.GetName(), w: c.GetWidth(), h: c.GetHeight(), anchor: [from.col, from.row], id: c.Chart.Id };
      } catch (e) { out[types[i]] = "THROW " + String(e).slice(0, 150); }
    }
    out.count = s.GetAllCharts().length;
    return out;
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
Came back:
```
bar            {'type': 'bar', 'name': 'agent:chart:bar', 'w': 4572000, 'h': 2743200, 'anchor': [8, 9], 'id': '1_15'}
column         THROW TypeError: Cannot set properties of null (setting 'x')
horizontalBar  {'type': 'horizontalBar', … 'anchor': [8, 41], 'id': '1_175'}
lineNormal     {'type': 'lineNormal', … 'id': '1_335'}
line           THROW TypeError: Cannot set properties of null (setting 'x')
pie / doughnut / area / scatter / barStacked   {'type': <same>, …}
lineNormalMarker {'type': 'unknown', …}        (created; GetChartType cannot name it)
count 10                                        (1 from the file + 9 added: the throws added nothing)
```
All accepted strings (from the source): `bar barStacked barStackedPercent bar3D barStacked3D barStackedPercent3D
barStackedPercent3DPerspective horizontalBar horizontalBarStacked horizontalBarStackedPercent horizontalBar3D
horizontalBarStacked3D horizontalBarStackedPercent3D lineNormal lineStacked lineStackedPercent lineNormalMarker
lineStackedMarker lineStackedPerMarker line3D pie pie3D doughnut scatter scatterLine scatterLineMarker scatterSmooth
scatterSmoothMarker stock area areaStacked areaStackedPercent comboCustom comboBarLine comboBarLineSecondary radar
radarMarker radarFilled`; anything else → null type → the TypeError above. The agent's spec names map as
`column→bar, bar→horizontalBar, line→lineNormal` (the same table `word.js` uses, `CHART_TYPES`).

#### Code: the person's editor sees names and ids; find by name; remove
```js
// person's editor, same key:
function(){ return Api.GetSheet("Data").GetAllCharts().map(function(c){ return [c.GetName(), c.Chart.Id, c.GetChartType(), String(c.GetTitle()).trim()]; }); }
// -> [['Chart 1','13','bar','Sales by region'], ['agent:chart:bar','1_15','bar','T bar'], …, ['agent:chart:pie','1_499','pie','T pie'], …]
// agent's editor:
function(){ var o={}; var d=Api.GetActiveWorkbook().GetDrawingsByName(["agent:chart:pie"]); o.byName=d.length;
  var r=d[0].Chart.deleteDrawingBase(); o.ret=String(r); o.after=Api.GetSheet("Data").GetAllCharts().length; return o; }
// -> {"byName": 1, "ret": "4", "after": 9}; the person's editor counted 9 charts on its first poll after.
```
- Internal ids (`Chart.Id`, `"1_15"`: creator index + counter, as in Word) are the same in both editors, but nothing
  public looks a chart up by id: `Api.GetByInternalId("1_499")` returned a wrapper `{Drawing}` even after the chart
  was deleted. A name the agent sets is the public handle; the outline returns `name` per chart. (Names are not
  unique by force: the runtime should pick `"<title> (agent N)"`-style names or check before setting.)
- `bInRows=false`: each column of the range is a series, the first column the categories, the first row the series
  names. Checked on `agent:chart:bar` (internal `Chart.getAllSeries()`): 2 series, names `Data!$B$2`, `Data!$C$2`,
  categories `Data!$A$3:$A$7`, values `Data!$B$3:$B$7` and `Data!$C$3:$C$7`.
- `GetWidth()/GetHeight()` return EMU (4572000 = 480 px).
- A file's chart title comes back with a trailing `"\r\n"` (`"Sales by region\r\n"`): trim.
- `AddChart` throws `"Cannot modify protected sheet"` on a sheet protected against objects (source).

---

### Q6. Sheets

**Answer:** `Api.AddSheet(name)` inserts the sheet **right after the agent's active sheet** and makes it the agent's
active sheet; it returns `undefined` (get it with `Api.GetSheet(name)`) and throws on a duplicate name. There is no
"after <name>" argument: add, then `sheet.Move(before, after)` (exactly one of the two is an `ApiWorksheet`), or
`target.SetActive()` before adding. Rename: `sheet.SetName(new)`, synchronous. Delete: `sheet.Delete()`. **With a
co-editor connected, `AddSheet`, `Delete` and `Move` are asynchronous**: they ask the server for the
sheet/workbook lock and apply when it answers, after the command has returned (alone in the document they apply
in-command). Deleting the last visible sheet is silently refused. The person's active sheet does not change when
the agent adds, renames or moves sheets; when the agent deletes the sheet the person is on, the person moves to a
neighbour.

#### Code
```js
// alone in the document (cell/q1d.js era, key e27b…): AddSheet is in-command
function(){ Api.AddSheet("Empty"); var s = Api.GetSheet("Empty"); return { used: s.GetUsedRange().GetAddress(false,false,"xlA1",false) }; }
// -> {"used": "A1"}         (q1d: AddSheet("Big") with Sales active -> sheets ["Sales","Big","Notes"], active "Big")

// with the person connected (cell-rich.xlsx; the person first ran Api.GetSheet("Locked").SetActive())
function(){ var r = Api.AddSheet("Plan"); var s = Api.GetSheet("Plan");
  return { ret: String(r), found: !!s, sheets: Api.GetSheets().map(function(x){return x.GetName()}), active: Api.GetActiveSheet().GetName() }; }
// -> {"ret": "undefined", "found": false, "sheets": ["Data","Hidden","Locked"], "active": "Data"}
// next command, agent:  {'sheets': ['Data','Plan','Hidden(hidden)','Locked'], 'active': 'Plan'}
//               person: {'sheets': ['Data','Plan','Hidden(hidden)','Locked'], 'active': 'Locked'}
```
```js
// cell/q6ops.js, person connected
function () {
  var names = function () { return Api.GetSheets().map(function (x) { return x.GetName(); }); }, out = {};
  Api.GetSheet("Plan").SetName("Budget");               out.afterRename = names();
  Api.GetSheet("Budget").Move(null, Api.GetSheet("Locked")); out.afterMove = names();
  out.delRet = String(Api.GetSheet("Data").Delete());    out.afterDelete = names();
  return out;
}
// -> {'afterRename': ['Data','Budget','Hidden','Locked'],      (rename: in-command)
//     'afterMove':   ['Data','Budget','Hidden','Locked'],      (move: not yet)
//     'delRet': 'undefined', 'afterDelete': ['Data','Budget','Hidden','Locked']}   (delete: not yet)
// next command, both editors: ['Hidden','Locked','Budget']; agent active 'Budget', person active 'Locked'
```
- Then `Api.GetSheet("Locked").Delete()` (the person's active sheet): both editors `['Hidden(hidden)','Budget']`,
  the person's active sheet became `Budget`; the person's page stayed alive (`window.dead` null).
- Then `Api.GetSheet("Budget").Delete()` (last visible sheet, a hidden one remains): nothing happened in either
  editor, no error. The source (`Workbook.removeWorksheet`) returns `-1` when no other visible sheet exists;
  `asc_deleteWorksheet` also refuses deleting every sheet, a protected workbook, or while the global lock is set.
- Lock latency on DEV (`cell/locklat.py`, AddSheet inside a group, polled until present): 40 ms, 4, 6, 42, 5 ms
  (in-command when the agent already held the lock from its previous change).

#### Gotchas
- The runtime must pre-check what the editor refuses silently: a duplicate name (AddSheet throws), deleting the last
  visible sheet, a structure-protected workbook (Q10).
- Because add/delete/move may land after the command, a batch that adds a sheet and then writes into it cannot run
  in one command when the person is connected; see Q7 for the safe order.
- `SetName` with a name that already exists: not tested; the runtime should check `Api.GetSheet(new)` first.

---

### Q7. All or nothing

**Answer:** **Yes for everything that applies in-command** (values, formulas, formats, column widths, chart add,
chart rename, chart delete via `deleteDrawingBase`, sheet rename): a cancelled group leaves nothing in either
editor (the person never sees it), a committed group reaches the person (~0.7 s). **Sheet add/delete/move with the
person connected are the exception**: they land asynchronously, and if the group ends before the lock answer
arrives they escape it: a **cancelled** batch still added the sheet, visibly to the person. Held open until they
land (tens of ms), they are inside the group and cancel cleanly. The editor's global lock is not set during a group
in the spreadsheet editor (`collaborativeEditing.getGlobalLock()` = 0).

#### Code: cancelled, then committed (`cell/q7edit.js`, person connected, key 2655…)
```js
function () {
  try {
    var s = Api.GetSheet("Sales");
    s.GetRange("A5:D5").SetValue([["West", 50, 60, "=B5+C5"]]);
    s.GetRange("F6").SetValue(scope.tag);
    s.GetRange("B2:C5").SetBold(true);
    s.SetColumnWidth(0, 18);
    var c = s.AddChart("'Sales'!$A$1:$C$5", false, "bar", 2, 480 * 9525, 288 * 9525, 6, 0, 1, 0);
    c.SetName("agent:" + scope.tag); c.SetTitle("By region", 12);
    Api.GetSheet("Notes").SetName("Notes-" + scope.tag);
    return { ok: true };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
```
method StartAction ["GroupActions"]; cmd {"tag":"cancelled"} -> {"ok":true}
-- inside the open group:
 agent  {"sheets": ["Sales","Notes-cancelled"], "a5": [["West","50","60","110"]], "f6": "cancelled", "bold": true, "w": 18, "charts": ["agent:cancelled"]}
 person {"sheets": ["Sales","Notes"], "a5": [["","","",""]], "f6": "", "bold": false, "w": 8.43, "charts": []}
method EndAction ["GroupActions","","cancel"]
-- after cancel (and again 3 s later, person):
 agent  {"sheets": ["Sales","Notes"], "a5": [["","","",""]], "f6": "", "bold": false, "w": 8.43, "charts": []}
 person {"sheets": ["Sales","Notes"], "a5": [["","","",""]], "f6": "", "bold": false, "w": 8.43, "charts": []}
== committed: StartAction; cmd {"tag":"committed"}; EndAction ["GroupActions",""]
person saw it after 715 ms (poll 2):
 {"sheets": ["Sales","Notes-committed"], "a5": [["West","50","60","110"]], "f6": "committed", "bold": true, "w": 18, "charts": ["agent:committed"]}
```
Chart delete + AddSheet in a group held open 2 s, then cancelled:
```
cmd -> {"addedNow": false, "del": "0", "charts": 0}
inside group: agent sheets ["Sales","G1","Notes-committed"], charts []; person ["Sales","Notes-committed"], charts ["agent:committed"]
after cancel: both ["Sales","Notes-committed"], charts ["agent:committed"]
```
Delete + Move in a group held open 2 s, then cancelled: inside, agent `["R2","Sales","Notes-committed"]`, person
unchanged; after cancel both `["Sales","Notes-committed","R1","R2"]`.

#### Code: the race (`cell/race.py`: StartAction, command, EndAction at once, as `office.ts` `inGroup` does)
```
== AddSheet, cancel at once        cmd {'now': False}   group ended after 33 ms
 +0s agent ['Sales','Notes-committed','R1'] | person ['Sales','Notes-committed']
 +1s agent ['Sales','Notes-committed','R1'] | person ['Sales','Notes-committed','R1']     <- cancelled, yet added
== AddSheet, commit at once        cmd {'now': False}   group ended after 43 ms
 +1s agent [..., 'R1', 'R2'] | person [..., 'R1', 'R2']
```

#### Design consequence
Run a batch in one group, but in phases: (1) a command with the sheet ops only, which returns what it asked for
(`{pending: [{add: "Plan"}, {delete: "Old"}]}`); (2) poll, still inside the group, with a read-only command until
each has landed (`Api.GetSheet(name)` present/absent, index moved), timeout ~3 s → cancel and refuse ("someone else
is changing the workbook's sheets; try again"); (3) the command with the cell/format/chart ops; (4) commit. Alone in
the document, phase 2 succeeds at once.

#### Gotchas
- `EndAction` of a committed group took up to ~1.6 s once (1791999494869 → 1791999496470): the commit sends
  everything then.
- After a cancel the agent's active sheet can move (it ended on `Notes-committed`): never rely on the agent's active
  sheet; address sheets by name.

---

### Q8. Flush

**Answer:** **the host page's `onDocumentStateChange(false)` after the edit is the flush signal, as in Word**
(`window.waitFlushed(since)` in runtime-office's host page works unchanged): it fired 11–18 ms after the command (or
the group's commit) returned for a small edit, ~550 ms after for 10,000 cells. A Command Service `forcesave` sent right after
it answered `error: 0`, and the status-6 callback (2.7–2.9 s on DEV) carried the `userdata` and a file holding the
edit. **But the pre-check `office.ts` does must change for cells**: the modified flag read from a command is always
`true` (the spreadsheet `asc_isDocumentModified()` includes `History.Have_Changes()`, and a command is itself an
open history point), so an edit that changes nothing (no event fires) would wait the full 10 s and report
`flushed: false`. Read the flag outside a command instead (`frame.evaluate(() => Asc.editor.asc_isDocumentModified())`,
internal), or skip the pre-check when the command reports it changed nothing.

#### Code (`cell/flush.py`: command in a committed group, `window.waitFlushed`, forcesave with JWT, callback, openpyxl)
```
cmd True; command(+group) done +41 ms; waitFlushed=True at +69 ms; events [[33,"onDocumentStateChange",true],[59,"onDocumentStateChange",false]]
forcesave {'key': '26554ec5f153ad08b62f80b78b50e978', 'error': 0} in 62 ms
status 6 after 2679 ms; userdata={"op": "spike-flush", "tag": "flush-1"}; users=['agent:spike']; forcesavetype=0
saved/26554ec5f153ad08b62f80b78b50e978-12.xlsx H1 = 'flush-1' …
```
Without a group:
```
cmd True; command(+group) done +30 ms; waitFlushed=True at +57 ms; events [[24,"onDocumentStateChange",true],[41,"onDocumentStateChange",false]]
forcesave {'key': '…', 'error': 0} in 60 ms
status 6 after 2878 ms; userdata={"op": "spike-flush", "tag": "flush-2"}; users=['agent:spike']; forcesavetype=0
saved/26554ec5f153ad08b62f80b78b50e978-13.xlsx H1 = 'flush-2' H2 = '=LEN(H1)' | sheets ['R2', 'Sales', 'Notes-committed']
```
The flag, right after (`cell/evwrite.py` and direct reads):
```
in a command:  Api.GetSheets()[0].worksheet.workbook.oApi.asc_isDocumentModified()   -> true
frame eval:    window.Asc.editor.asc_isDocumentModified()                               -> false
same value written again (A2 = "North"): no event, waitFlushed(3 s)=False, frame flag false
read-only command:                      no event, waitFlushed(3 s)=False, frame flag false
10,000 cells (J1:BG200):                events [[151,true],[717,false]], waitFlushed=True, frame flag false
```
- The person's editor reached `BG200 = 9751` within ~6 s of that big write (empty on an immediate poll).
- The person's page fires `onDocumentStateChange` true/false pairs too in the spreadsheet editor, at moments the
  person made no edit (unlike Word, where only the editing page fires); only the agent page's events matter.
- The "already found" observation (flag true in both editors a minute later) is the in-command read, not the state.

---

### Q9. Two writers

**Answer: the builder's writes take no lock, and two writers on the same cell DIVERGE: each editor ends with the
other's value, and the person's editor then shows a value that is not in the document.** Same when the agent writes
a cell the person is typing in (the person's open cell editor holds a server lock the builder ignores): after the
person commits, the person sees the agent's value, the document (agent, a freshly opened third editor) holds the
person's. Nothing errors, nothing is refused. The spreadsheet editor does lock ranges, but only on the UI path
(`WorksheetView._isLockedCells`), so **the agent must take the same range locks itself**: inside the open group,
ask `_isLockedCells` for every target range (internal, asynchronous), wait for the answer, and refuse the batch when
it is refused. A lock the agent holds inside an open group is kept until the commit, and the person's editor then
cannot open a cell editor on that range; a person's lock makes the agent's ask refused at once.

#### Code: both write B6 at the same moment (`cell/twowriters.py`, two threads, one builder command each)
```js
function(){ try { Api.GetSheet("Sales").GetRange(scope.c).SetValue(scope.v); return Api.GetSheet("Sales").GetRange(scope.c).GetValue(); } catch(e) { return {__error:String(e)}; } }
```
```
0 own reads {'agent': 'agent-0', 'person': 'person-0'} | after 2.5 s: agent person-0 person agent-0
1 own reads {'agent': 'agent-1', 'person': 'person-1'} | after 2.5 s: agent person-1 person agent-1
2 …agent person-2 person agent-2        3 …agent person-3 person agent-3
after 12 s: agent "person-3", person "agent-3"; a third editor opened fresh on the key: "person-3"
```

#### Code: the agent writes a cell the person is typing in
Person's cell editor opened through internals (`eval … frame` in the person's page):
```js
(() => { const api = Asc.editor, wb = api.wb; api.asc_showWorksheet(api.wbModel.getWorksheetByName('Sales').getIndex());
  const ws = wb.getWorksheet(); ws.setSelection(new Asc.Range(1, 6, 1, 6));                  // B7
  const o = new AscCommonExcel.CEditorEnterOptions(); o.newText = 'typing'; o.focus = true; wb._onEditCell(o);
  return JSON.stringify({ mode: api.asc_getCellEditMode(), sel: ws.model.selectionRange.getLast().getName() }); })()
// -> {"mode":true,"sel":"B7"}; a moment later the person's own locks: 1, cell editor text "typing"
```
The agent sees the lock (`cell/q9lock.js`):
```js
function () {
  try {
    var s = Api.GetSheet(scope.sheet), ws = s.worksheet, api = ws.workbook.oApi, ce = api.collaborativeEditing, box = s.GetRange(scope.range).range.bbox;
    var info = ce.getLockInfo(AscCommonExcel.c_oAscLockTypeElem.Range, null, ws.getId(), new AscCommonExcel.asc_CCollaborativeRange(box.c1, box.r1, box.c2, box.r2));
    var other = ce.getLockIntersection(info, AscCommon.c_oAscLockTypes.kLockTypeOther, false);
    return { lockedByOther: !!other, otherUser: other ? (other.UserId || "?") : null };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
// B7    -> {"lockedByOther": true, "otherUser": "user:spike3"}   (connection id = user id + index)
// A1:C3 -> {"lockedByOther": false, "otherUser": null}
```
Then:
```
agent: Api.GetSheet("Sales").GetRange("B7").SetValue("agent-over-lock") -> {"ret": true, "now": "agent-over-lock"}
+2 s:  agent "agent-over-lock" | person "" (still typing "typing") | third "agent-over-lock"
person commits (Asc.editor.wb.closeCellEditor()):
+3 s:  agent "typing" | person "agent-over-lock" | third "typing"          <- the person sees what is not saved
```

#### Code: the agent takes the lock first (`cell/q9take.js`)
```js
function () {
  try {
    // ask the server for the range locks the editor's own cell edit takes; the answer arrives after this command
    var s = Api.GetSheet(scope.sheet), ws = s.worksheet, api = ws.workbook.oApi, view = api.wb.getWorksheet(ws.getIndex());
    var boxes = scope.ranges.map(function (a) { return s.GetRange(a).range.bbox; });
    var store = Api.__agentLocks || (Api.__agentLocks = {});
    store[scope.ticket] = "pending";
    view._isLockedCells(boxes, null, function (ok) { store[scope.ticket] = ok ? "granted" : "refused"; });
    return { asked: store[scope.ticket], ownLocks: api.collaborativeEditing.getOwnLocksLength() };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
```
outside a group, free range B8:C8:  asked "pending" -> next command "granted", own locks 0 (released by the next save
                                    tick); the person then opened a cell editor on B8 normally (mode true, own 1)
person editing B8, agent asks B8:C8: asked "refused" (synchronously: the agent's editor knows the person's lock)
inside a group (StartAction first), B9: "pending" -> "granted", own locks 1, 1, 1 over 1.5 s;
  person tries to edit B9: cell editor does not open (mode false, own 0)
  agent writes B9 = "agent-B9", EndAction commit: +2 s agent "agent-B9", person "agent-B9", agent own locks 0
```
- `Api.__agentLocks` survives between commands (`Api` is the same object), which is how the answer is read back.

#### Design consequence
Per batch, inside one group: (1) ask range locks for every `set`/`format` target (and the sheet ops of Q6), (2)
poll with a read-only command until every ticket is `granted`/`refused` (DEV: the next command, ~100 ms), (3) on
any refusal: cancel, refuse "Flavius is editing Sales!B7 right now", (4) guards + writes, (5) commit, flush. The
outline/view can report locked ranges with `getLockIntersection(…, kLockTypeOther)` and the user's name from the
participants list:
```js
// who has the document open (internal): Api.GetSheets()[0].worksheet.workbook.oApi.CoAuthoringApi._CoAuthoringApi._participants
// -> [{'conn': 'agent:spike1', 'id': 'agent:spike', 'name': 'Agent', 'view': False},
//     {'conn': 'user:spike3', 'id': 'user:spike', 'name': 'Flavius', 'view': False},
//     {'conn': 'user:third6', 'id': 'user:third', 'name': 'Third', 'view': False}]
// (the public-looking CoAuthoringApi.getUsers() returned [])
```

#### Gotchas
- Everything here is internal (`_isLockedCells`, `collaborativeEditing`, `_participants`): no builder method takes
  or reports a lock. It is the editor's own UI path, so it is the behaviour the person's editor expects.
- Only cell ranges were tested; formats use the same range lock in the UI; chart add/delete use drawing locks
  (not probed).
- The same divergence presumably applies between the person's builder plugins and the agent; the UI path (typing,
  paste, delete) always takes the lock, so a locked agent write is safe against it.

---

### Q10. Protection

**Answer:** detect with internals before editing: sheet protection `ws.getSheetProtection()` (true/false) plus
per-option getters on `ws.sheetProtection` (`asc_getFormatCells()`, `asc_getFormatColumns()`, `asc_getObjects()`, …:
`true` = that action is protected), locked cells `ws.isIntersectLockedRanges([bbox])` (what `SetValue` itself
checks), workbook structure `api.asc_isProtectedWorkbook()`. The builder enforces some of it silently
(`SetValue` → `false`, format setters → `null`, sheet delete/move do nothing) and **bypasses the rest**:
`SetColumnWidth` on a sheet whose column formatting is protected, and **`AddSheet`/`SetName` on a structure-locked
workbook, both applied**. So the runtime refuses up front, with a reason, instead of trusting the builder.

#### Code: protected sheet (`fixtures/cell-rich.xlsx` sheet `Locked`: `ws.protection.sheet = True`, `B3` unlocked) (`cell/q10sheet.js`)
```js
function () {
  try {
    var s = Api.GetSheet("Locked"), ws = s.worksheet, out = {};
    function t(name, f) { try { var v = f(); out[name] = v === undefined ? "undefined" : v; } catch (e) { out[name] = "THROW " + String(e).slice(0, 100); } }
    out.protected = ws.getSheetProtection();
    var b2 = s.GetRange("B2").range.bbox, b3 = s.GetRange("B3").range.bbox;
    out.lockedB2 = ws.isIntersectLockedRanges([b2]);
    out.lockedB3 = ws.isIntersectLockedRanges([b3]);
    out.lockedB2B3 = ws.isIntersectLockedRanges([s.GetRange("B2:B3").range.bbox]);
    t("setB2", function () { return s.GetRange("B2").SetValue(2000); });
    t("setB3", function () { return s.GetRange("B3").SetValue(6); });
    t("setB2B3", function () { return s.GetRange("B2:B3").SetValue([[2001], [7]]); });
    t("bold", function () { return s.GetRange("A1").SetBold(true); });
    t("numfmt", function () { return s.GetRange("B3").SetNumberFormat("0.00"); });
    t("colWidth", function () { return s.SetColumnWidth(0, 25); });
    t("chart", function () { var c = s.AddChart("'Locked'!$A$1:$B$2", false, "bar", 2, 3e6, 2e6, 3, 0, 1, 0); return c ? "added" : "null"; });
    t("rename", function () { return s.SetName("Locked2"); });
    var p = ws.sheetProtection;
    t("opts", function () { return { sheet: p.asc_getSheet(), formatCells: p.asc_getFormatCells(), objects: p.asc_getObjects(), pwd: !!(p.asc_isPassword && p.asc_isPassword()) }; });
    return out;
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
Came back, then the state in the next command:
```
{"protected": true, "lockedB2": true, "lockedB3": false, "lockedB2B3": true,
 "setB2": false, "setB3": true, "setB2B3": false, "bold": null, "numfmt": null,
 "colWidth": "undefined", "chart": "added", "rename": "undefined",
 "opts": {"sheet": true, "formatCells": true, "objects": false, "pwd": false}}
next: {'name': 'Locked2', 'b2': '1000', 'b3': '6', 'a1bold': False, 'b3fmt': 'General', 'w': 25, 'charts': 1}
```
- Locked `B2` refused, unlocked `B3` written, a range spanning both refused whole; bold and number format not
  applied; **column width applied (25) although `asc_getFormatColumns()` is `true`**; chart added because openpyxl's
  default leaves objects unprotected (`objects: false`; `AddChart` throws when objects are protected, source);
  rename applied (sheet protection does not cover the name, as in Excel).
- All option getters on `Locked` (`true` = protected): `Sheet, FormatCells, FormatColumns, FormatRows,
  InsertColumns, InsertRows, InsertHyperlinks, DeleteColumns, DeleteRows, Sort, AutoFilter, PivotTables: true`;
  `Objects, Scenarios, SelectLockedCells, SelectUnlockedCells: false`. An unprotected sheet:
  `getSheetProtection()` false and `sheetProtection` null.

#### Code: structure-locked workbook (`fixtures/cell-wbprot.xlsx`, `WorkbookProtection(lockStructure=True)`)
```js
function(){ try { var api = Api.GetSheets()[0].worksheet.workbook.oApi, o = {};
  o.isProt = String(api.asc_isProtectedWorkbook());
  var wp = api.wbModel.workbookProtection; o.wp = wp ? { lockStructure: wp.asc_getLockStructure(), lockWindows: wp.asc_getLockWindows() } : null;
  try { Api.AddSheet("New"); o.add = "ok"; } catch (e) { o.add = "THROW " + e; } o.addNow = !!Api.GetSheet("New");
  try { Api.GetSheet("Only").SetName("Renamed"); o.rename = "ok"; } catch (e) { o.rename = "THROW " + e; }
  o.names = Api.GetSheets().map(function(s){ return s.GetName(); }); return o; } catch (e) { return { __error: String(e && e.stack || e) }; } }
// -> {'isProt': 'true', 'wp': {'lockStructure': True, 'lockWindows': None}, 'add': 'ok', 'addNow': True, 'rename': 'ok', 'names': ['Renamed', 'New']}
// next command: ['Renamed', 'New']
// then Api.GetSheet("New").Delete() and .Move(Api.GetSheet("Renamed"), null): {'del': 'undefined', 'names': ['Renamed', 'New']}, unchanged next command
```
(`_addWorksheetsCheck`, source: `this.asc_isProtectedWorkbook() && callback(false)`, then asks the lock and adds
anyway; `asc_deleteWorksheet`/`asc_moveWorksheet` return early on `asc_isProtectedWorkbook()`.)

#### Gotchas
- Refuse in the script before any write: `set` on a range intersecting locked cells of a protected sheet, `format`
  on a protected sheet (`formatCells`; `formatColumns` for widths), `chart` when `objects` is protected, any `sheet`
  op on a structure-locked workbook. Word's script does the same with its `protection()` check.
- Password presence (`asc_isPassword`) was not exercised (the fixture has no password); only the person can lift
  protection either way.

---

### Saved file check (forcesave of key 2655…, `saved/26554ec5f153ad08b62f80b78b50e978-13.xlsx`, openpyxl + unzip)
- The agent's chart: `<c:barDir val="col"/>` (so builder `bar` = vertical columns), anchor
  `<xdr:from><xdr:col>6</xdr:col>…<xdr:row>1</xdr:row>` (G2) with `<xdr:ext cx="4572000" cy="2743200"/>`, and
  **its name survives in the file**: `<xdr:cNvPr id="100534453" name="agent:committed">`, so a name the agent sets is
  a handle that also survives reopen (internal ids do not, as in Word).
- Column A width set to 18 is saved as `18.7109375` (Excel's padded width; openpyxl `column_dimensions['A'].width`);
  `B2` bold `True`, `A5` `West`, `D5` `=B5+C5` (formula kept), `F6` `committed`.

---

### Open questions
- **Chart and format locks under co-editing.** Q9 proved range locks for cell values only. Whether `format` should
  ask the same range lock (the UI does) and whether chart add/delete need drawing locks (`objectRender` /
  `c_oAscLockTypeElem.Object`) was not probed; a concurrent person edit of a chart was not tried.
- **Lock latency under load.** Lock answers came back in the next command (~100 ms) and sheet locks in 4–42 ms on
  an idle DEV; a busy server or a person holding the workbook lock (e.g. mid-rename) was not simulated, so the
  timeout for the lock phase (suggested 3 s) is a guess.
- **Divergence repair.** If a divergence happens anyway (a person's builder plugin, or a write that skipped the
  lock), the person's editor keeps showing the wrong value; whether `drop_editors` (reload) is the only repair was
  not tested.
- **`SetName` to an existing sheet name**, and names with characters Excel forbids (`[]:*?/\`), were not tested; the
  runtime should validate before calling.
- **Number-format validation**: the editor stores invalid codes; no internal validator was looked for (candidate:
  `AscCommon.oNumFormatCache.get(code)`), so the plan should validate in the runtime or report the resulting text.
- **Find** was shown broken for this use (false hits, misses), not debugged; the scan replaces it.

## Slide decks

Probe port 8793. Every command below ran through `pc.py cmd <id> @slide/<file>.js` (the scripts are kept in
`slide/`). Agent page = `agent:spike`, person page = `user:spike` ("Flavius"), both on one fresh key.
Fixture `fixtures/sample.pptx`: 13.333x7.5 in (12191695 x 6858000 EMU), slide 1 "Title and Content", slide 2
"Title Only" + a text box + notes.

### What most affects the design

1. **Slide ids**: `slide.Slide.Get_Id()` (internal), same in both editors, stable under others' add/move/delete;
   no `GetByInternalId` for slides (scan `GetAllSlides()`). Replace **in place** keeps the id. Durable across reopen
   only via names: shape `SetName`, slide `Slide.setCSldName` (both round-trip in the saved pptx).
2. **Native build works** (Builder API + 4 internal calls: `Shape.setStyle(null)`, `ParaHyperlink.Add_ToContent`,
   `Paragraph.Set_PresentationLevel`, table grid/cell widths/`Row.Set_Height`/`xfrm.setExtX`); order traps:
   `GetDocContent()` before `SetPaddings`, table style before formatting, level after other paragraph setters.
3. **Text layout matches Chromium to 0.01 px** with the server's own fonts + `font-kerning:none;
   font-variant-ligatures:none; text-rendering:geometricPrecision; line-height:1.2`. Server fonts are fetchable
   (`/fonts/<id>`, first 32 bytes XOR a fixed key). Canvas px = 0.5 pt (32 px = 16 pt).
4. **GroupActions is a real transaction in slides** (cancel leaves nothing in either editor, person's edits kept).
   **Flush: wait for `onDocumentStateChange(false)` after `EndAction`; `isDocumentModified()` reads false *before*
   the send** (runtime-office's flag pre-check would report flushed too early). Forcesave then works (status 6 ~4 s).
5. **No locks in fast mode**: the agent can replace/delete a slide the person is typing in; the person's
   selection silently drops. Guard with a content hash, not locks. SVG pictures go in directly (svgBlip + PNG fallback).
   Command results deeper than 10 levels vanish: return views as JSON strings. `Asc` is shadowed in commands.

### Q1. Enumerate

**Answer:** the slide editor's Builder API is the Presentation API (`Api.GetPresentation()`); slides have **no**
`GetInternalId` (shapes, tables, images and masters do); `Api.GetByInternalId` resolves drawings but not slides.

Ran `slide/enum1.js` and `slide/enum2.js` (the `methods()` helper from `enumlib.js`, prototype walk with property
descriptors). Full lists (verbatim from the editor):

- **Api**: `AI` `AddComment` `AutoColor` `CentimetersToPoints` `CoAuthoringChatSendMessage` `ConvertDocument` `CreateBlipFill` `CreateBlockLvlSdt` `CreateBullet` `CreateChart` `CreateCheckBoxContentControl` `CreateComboBoxContentControl` `CreateCustomGeometry` `CreateDatePickerContentControl` `CreateDefaultMasterSlide` `CreateDropDownListContentControl` `CreateGradientStop` `CreateGroup` `CreateHyperlink` `CreateImage` `CreateInlineLvlSdt` `CreateLayout` `CreateLinearGradientFill` `CreateMaster` `CreateNoFill` `CreateNumbering` `CreateOleObject` `CreateParaPr` `CreateParagraph` `CreatePatternFill` `CreatePictureContentControl` `CreatePlaceholder` `CreatePresetColor` `CreatePresetGeometry` `CreateRGBColor` `CreateRadialGradientFill` `CreateRange` `CreateRun` `CreateSchemeColor` `CreateShape` `CreateSlide` `CreateSlideShowTransition` `CreateSolidFill` `CreateStroke` `CreateTable` `CreateTableCellPr` `CreateTablePr` `CreateTableRowPr` `CreateTableStylePr` `CreateTextPr` `CreateTheme` `CreateThemeColorScheme` `CreateThemeFontScheme` `CreateThemeFormatScheme` `CreateWordArt` `EmusToMillimeters` `EmusToPoints` `FromJSON` `GetByInternalId` `GetDocument` `GetFullName` `GetMailMergeReceptionsCount` `GetMailMergeTemplateDocContent` `GetPresentation` `GetSelection` `HexColor` `InchesToPoints` `LinesToPoints` `LoadMailMergeData` `MailMerge` `MillimetersToEmus` `MillimetersToPixels` `MillimetersToPoints` `Mm2Px` `PicasToPoints` `PixelsToEmus` `PixelsToPoints` `PointsToCentimeters` `PointsToEmus` `PointsToInches` `PointsToLines` `PointsToMillimeters` `PointsToPicas` `PointsToPixels` `PointsToTwips` `Px2Emu` `RGB` `RGBA` `ReplaceDocumentContent` `ReplaceTextSmart` `Save` `ThemeColor` `TwipsToPoints` `attachEvent` `detachEvent` `installDeveloperPlugin` `private_CreateApiChart` `private_CreateApiComment` `private_CreateApiDocContent` `private_CreateApiFill` `private_CreateApiGradStop` `private_CreateApiHyperlink` `private_CreateApiLayout` `private_CreateApiMaster` `private_CreateApiParaPr` `private_CreateApiParagraph` `private_CreateApiPresentation` `private_CreateApiRun` `private_CreateApiSlide` `private_CreateApiStroke` `private_CreateApiTableCellPr` `private_CreateApiTablePr` `private_CreateApiTableRowPr` `private_CreateApiTableStylePr` `private_CreateApiTextPr` `private_CreateApiUniColor` `private_CreateCheckBoxForm` `private_CreateComboBoxForm` `private_CreateComplexForm` `private_CreateDateForm` `private_CreateGeometry` `private_CreatePictureForm` `private_CreateTextForm` `private_CreateTextPr` `private_checkPlaceholders` `private_createWordArt` `sendEvent`

- **ApiPresentation**: `AddMaster` `AddMathEquation` `AddSlide` `ApplyTheme` `CreateNewHistoryPoint` `GetAllCharts` `GetAllComments` `GetAllDrawings` `GetAllImages` `GetAllOleObjects` `GetAllShapes` `GetAllSlideMasters` `GetAllSlides` `GetAllTables` `GetClassType` `GetCore` `GetCurSlideIndex` `GetCurrentSlide` `GetCurrentVisibleSlide` `GetCustomProperties` `GetCustomXmlParts` `GetDocumentInfo` `GetDrawingsByName` `GetHeight` `GetLoopUntilStopped` `GetMaster` `GetMastersCount` `GetSlideByIndex` `GetSlidesCount` `GetWidth` `RemoveSlides` `ReplaceCurrentImage` `SetLanguage` `SetLoopUntilStopped` `SetSizes` `SlidesToJSON` `ToJSON` `_collectAllObjects`

- **ApiSlide**: `AddComment` `AddNotesText` `AddObject` `ApplyLayout` `ApplyTheme` `ClearBackground` `Copy` `Delete` `Duplicate` `FollowLayoutBackground` `FollowMasterBackground` `GetAllCharts` `GetAllDrawings` `GetAllImages` `GetAllOleObjects` `GetAllShapes` `GetAllTables` `GetClassType` `GetDrawingsByPlaceholderType` `GetHeight` `GetLayout` `GetNotesPage` `GetSlideIndex` `GetSlideShowTransition` `GetTheme` `GetTimeLine` `GetVisible` `GetWidth` `GroupDrawings` `MoveTo` `RemoveAllObjects` `RemoveObject` `Select` `SetBackground` `SetSlideShowTransition` `SetVisible` `ToJSON`

- **ApiShape**: `Copy` `Delete` `Fill` `GetClassType` `GetContent` `GetDocContent` `GetFill` `GetFlipH` `GetFlipV` `GetGeometry` `GetHeight` `GetHyperlink` `GetInternalId` `GetLine` `GetLockValue` `GetName` `GetParent` `GetParentLayout` `GetParentMaster` `GetParentSlide` `GetPlaceholder` `GetPosX` `GetPosY` `GetRotation` `GetWidth` `ReplacePlaceholder` `Select` `SetFill` `SetFlipH` `SetFlipV` `SetGeometry` `SetHyperlink` `SetLine` `SetLockValue` `SetName` `SetOutLine` `SetPaddings` `SetPlaceholder` `SetPosX` `SetPosY` `SetPosition` `SetRotation` `SetSize` `SetVerticalTextAlign` `ToJSON` `Unselect`

- **ApiDocContent (shape.GetDocContent())**: `AddElement` `GetAllCharts` `GetAllDrawingObjects` `GetAllImages` `GetAllOleObjects` `GetAllParagraphs` `GetAllShapes` `GetAllTables` `GetClassType` `GetContent` `GetCurrentContentControl` `GetCurrentParagraph` `GetCurrentRun` `GetDocumentVisitor` `GetElement` `GetElementsCount` `GetInternalId` `GetRange` `GetText` `IsEndnote` `IsFootnote` `Push` `RemoveAllElements` `RemoveElement` `SelectNoteReference` `ToJSON`

- **ApiParagraph**: `AddBookmarkCrossRef` `AddCaption` `AddCaptionCrossRef` `AddColumnBreak` `AddComment` `AddDrawing` `AddElement` `AddEndnoteCrossRef` `AddFootnoteCrossRef` `AddHeadingCrossRef` `AddHyperlink` `AddInlineLvlSdt` `AddLineBreak` `AddNumberedCrossRef` `AddPageBreak` `AddPageNumber` `AddPagesCount` `AddTabStop` `AddText` `Copy` `Delete` `GetAllCharts` `GetAllContentControls` `GetAllDrawingObjects` `GetAllImages` `GetAllOleObjects` `GetAllShapes` `GetClassType` `GetElement` `GetElementsCount` `GetFontNames` `GetIndFirstLine` `GetIndLeft` `GetIndRight` `GetInternalId` `GetJc` `GetLastRunWithText` `GetNext` `GetNumbering` `GetOutlineLvl` `GetParaId` `GetParaPr` `GetParagraphMarkTextPr` `GetParentContentControl` `GetParentTable` `GetParentTableCell` `GetPosInParent` `GetPrevious` `GetRange` `GetSection` `GetShd` `GetSpacingAfter` `GetSpacingBefore` `GetSpacingLineRule` `GetSpacingLineValue` `GetStyle` `GetText` `GetTextPr` `InsertInContentControl` `InsertParagraph` `Last` `OnChangeParaPr` `OnChangeTextPr` `Push` `RemoveAllElements` `RemoveElement` `ReplaceByElement` `Search` `Select` `SetBetweenBorder` `SetBold` `SetBottomBorder` `SetBullet` `SetCaps` `SetColor` `SetContextualSpacing` `SetDoubleStrikeout` `SetFontFamily` `SetFontSize` `SetHighlight` `SetIndFirstLine` `SetIndLeft` `SetIndRight` `SetItalic` `SetJc` `SetKeepLines` `SetKeepNext` `SetLeftBorder` `SetNumPr` `SetNumbering` `SetOutlineLvl` `SetPageBreakBefore` `SetParaId` `SetPosition` `SetReadingOrder` `SetRightBorder` `SetSection` `SetShd` `SetSmallCaps` `SetSpacing` `SetSpacingAfter` `SetSpacingBefore` `SetSpacingLine` `SetStrikeout` `SetStyle` `SetTabs` `SetTextPr` `SetTopBorder` `SetUnderline` `SetVertAlign` `SetWidowControl` `ToJSON` `WrapInMailMergeField` `private_GetImpl` `private_OnChange`

- **ApiRun**: `AddColumnBreak` `AddComment` `AddDrawing` `AddHyperlink` `AddLineBreak` `AddPageBreak` `AddTabStop` `AddText` `ClearContent` `Copy` `Delete` `GetBold` `GetCaps` `GetClassType` `GetColor` `GetDoubleStrikeout` `GetFill` `GetFontFamily` `GetFontNames` `GetFontSize` `GetHighlight` `GetItalic` `GetLanguage` `GetOutLine` `GetParentContentControl` `GetParentParagraph` `GetParentTable` `GetParentTableCell` `GetPosition` `GetRange` `GetShd` `GetSmallCaps` `GetSpacing` `GetStrikeout` `GetStyle` `GetText` `GetTextFill` `GetTextPr` `GetUnderline` `GetVertAlign` `MoveCursorToPos` `OnChangeTextPr` `RemoveAllElements` `Select` `SetBold` `SetCaps` `SetColor` `SetDoubleStrikeout` `SetFill` `SetFontFamily` `SetFontSize` `SetHighlight` `SetItalic` `SetLanguage` `SetOutLine` `SetPosition` `SetShd` `SetSmallCaps` `SetSpacing` `SetStrikeout` `SetStyle` `SetTextFill` `SetTextPr` `SetUnderline` `SetVertAlign` `ToJSON` `WrapInMailMergeField` `private_GetImpl` `private_GetNextInParent` `private_GetPreviousInParent` `private_OnChange`

- **ApiTable**: `AddColumn` `AddRow` `Copy` `Delete` `Fill` `GetClassType` `GetFlipH` `GetFlipV` `GetHeight` `GetHyperlink` `GetInternalId` `GetLockValue` `GetName` `GetParent` `GetParentLayout` `GetParentMaster` `GetParentSlide` `GetPlaceholder` `GetPosX` `GetPosY` `GetRotation` `GetRow` `GetWidth` `MergeCells` `OnChangeTablePr` `RemoveColumn` `RemoveRow` `ReplacePlaceholder` `Select` `SetFlipH` `SetFlipV` `SetHyperlink` `SetLockValue` `SetName` `SetOutLine` `SetPlaceholder` `SetPosX` `SetPosY` `SetPosition` `SetRotation` `SetShd` `SetSize` `SetTableLook` `ToJSON` `Unselect` `private_PrepareTableForActions`

- **ApiTableRow**: `GetCell` `GetCellsCount` `GetClassType` `SetHeight`

- **ApiTableCell**: `GetClassType` `GetContent` `SetCellBorderBottom` `SetCellBorderLeft` `SetCellBorderRight` `SetCellBorderTop` `SetCellMarginBottom` `SetCellMarginLeft` `SetCellMarginRight` `SetCellMarginTop` `SetShd` `SetTextDirection` `SetVerticalAlign`

- **ApiImage**: `Copy` `Delete` `Fill` `GetClassType` `GetFlipH` `GetFlipV` `GetHeight` `GetHyperlink` `GetInternalId` `GetLockValue` `GetName` `GetParent` `GetParentLayout` `GetParentMaster` `GetParentSlide` `GetPlaceholder` `GetPosX` `GetPosY` `GetRotation` `GetWidth` `ReplacePlaceholder` `Select` `SetFlipH` `SetFlipV` `SetHyperlink` `SetLockValue` `SetName` `SetOutLine` `SetPlaceholder` `SetPosX` `SetPosY` `SetPosition` `SetRotation` `SetSize` `ToJSON` `Unselect`

- **ApiLayout**: `AddObject` `ClearBackground` `Copy` `Delete` `Duplicate` `FollowMasterBackground` `GetAllCharts` `GetAllDrawings` `GetAllImages` `GetAllOleObjects` `GetAllShapes` `GetAllTables` `GetClassType` `GetDrawingsByPlaceholderType` `GetLayoutType` `GetMaster` `GetName` `GroupDrawings` `MoveTo` `RemoveObject` `SetBackground` `SetName` `ToJSON`

- **ApiMaster**: `AddLayout` `AddObject` `ClearBackground` `Copy` `Delete` `Duplicate` `GetAllCharts` `GetAllDrawings` `GetAllImages` `GetAllLayouts` `GetAllOleObjects` `GetAllShapes` `GetAllTables` `GetClassType` `GetDrawingsByPlaceholderType` `GetInternalId` `GetLayout` `GetLayoutByType` `GetLayoutsCount` `GetTheme` `GroupDrawings` `RemoveLayout` `RemoveObject` `SetBackground` `SetTheme` `ToJSON`

- **ApiNotesPage (slide.GetNotesPage())**: `AddBodyShapeText` `GetBodyShape` `GetBodyShapeText` `GetClassType` `GetTheme`

- **ApiPlaceholder**: `GetClassType` `GetIndex` `GetType` `SetIndex` `SetType`

- **ApiFill**: `GetClassType` `GetType` `ToJSON`

- **ApiStroke**: `GetClassType` `GetDashType` `GetFill` `GetWidth` `ToJSON`

- **ApiParaPr**: `GetClassType` `GetIndFirstLine` `GetIndLeft` `GetIndRight` `GetJc` `GetOutlineLvl` `GetShd` `GetSpacingAfter` `GetSpacingBefore` `GetSpacingLineRule` `GetSpacingLineValue` `GetStyle` `SetBetweenBorder` `SetBottomBorder` `SetBullet` `SetContextualSpacing` `SetIndFirstLine` `SetIndLeft` `SetIndRight` `SetJc` `SetKeepLines` `SetKeepNext` `SetLeftBorder` `SetNumPr` `SetOutlineLvl` `SetPageBreakBefore` `SetRightBorder` `SetShd` `SetSpacingAfter` `SetSpacingBefore` `SetSpacingLine` `SetStyle` `SetTabs` `SetTopBorder` `SetWidowControl` `ToJSON` `private_OnChange`

- **ApiTextPr**: `GetBold` `GetCaps` `GetClassType` `GetColor` `GetDoubleStrikeout` `GetFill` `GetFontFamily` `GetFontSize` `GetHighlight` `GetItalic` `GetLanguage` `GetOutLine` `GetPosition` `GetShd` `GetSmallCaps` `GetSpacing` `GetStrikeout` `GetStyle` `GetTextFill` `GetUnderline` `GetVertAlign` `SetBold` `SetCaps` `SetColor` `SetDoubleStrikeout` `SetFill` `SetFontFamily` `SetFontSize` `SetHighlight` `SetItalic` `SetLanguage` `SetOutLine` `SetPosition` `SetShd` `SetSmallCaps` `SetSpacing` `SetStrikeout` `SetStyle` `SetTextFill` `SetUnderline` `SetVertAlign` `ToJSON` `private_OnChange`

- **ApiBullet**: `GetClassType`

- **ApiHyperlink**: `GetClassType` `GetDisplayedText` `GetElement` `GetElementsCount` `GetLinkedText` `GetRange` `GetScreenTipText` `SetDefaultStyle` `SetDisplayedText` `SetLink` `SetScreenTipText` `ToJSON` `private_GetImpl`

Gotchas:
- `ApiSlide` has no `GetInternalId`; its CSlide is `slide.Slide` (only own key), whose `Get_Id()` is the internal id (see Q2).
- `ApiTableRow`/`ApiTableCell` have no getters for size/fill/borders: reading a table's styling needs `ToJSON` or internals (Q3).
- `ApiNotesPage` has `GetBodyShapeText`/`AddBodyShapeText`, `ApiSlide.AddNotesText`; no setter that replaces notes text (Q4).
### Q2. Identity

**Answer:** a slide's id is its CSlide internal id, `slide.Slide.Get_Id()` (no public getter): the same string in
both editors (load ids like `"1428"`, created ids `"1_43"` agent / `"2_19"` person), unchanged when other slides are
added, removed or moved, and when the slide itself moves. `Api.GetByInternalId` does **not** resolve slides (returns
null), so find a slide by scanning `pres.GetAllSlides()`. Shapes/tables/images: public `GetInternalId()`, same in both
editors, stable, and `Api.GetByInternalId(id)` resolves them.

`slide/ids1.js` on both pages at load:
```js
var s = pres.GetSlideByIndex(i), S = s.Slide;
rec.id = S.Get_Id();                                   // "1428", "1473"
var back = Api.GetByInternalId(String(rec.id));        // null for a slide
rec.shapes = s.GetAllDrawings().map(function (d) { var b = Api.GetByInternalId(String(d.GetInternalId())); ... });
```
```
agent:  [('1428', ['1437', '1453']), ('1473', ['1482', '1498'])]   byId: null for both slides, "shape" for every shape
person: [('1428', ['1437', '1453']), ('1473', ['1482', '1498'])]
```
The lookup used everywhere below:
```js
function slideById(id) {
  var all = pres.GetAllSlides();
  for (var i = 0; i < all.length; i++) if (all[i].Slide.Get_Id() === id) return all[i];
  return null;
}
```
Stability (`slide/person-moves.js`, run in the **person's** editor: `Api.CreateSlide()` + `pres.AddSlide(s, 0)`,
`slideById("1_402").MoveTo(1)`, `slideById("1_43").Delete()`), then `slide/list.js` (`[id, index, layout, shape ids]`)
on both pages:
```
agent before: ["1428",0,..,"1437,1453"],["1_630",1,..,"1_674,..,1_770"],["1_43",2,..],["1473",3,..],["1_402",4,..,"1_446,..,1_542"]
person ran:   {"added":"2_19","moved":true,"deleted":true}
agent after:  ["2_19",0,"Title Slide",""],["1_402",1,"Blank","1_446,1_456,1_480,1_534,1_542"],["1428",2,..,"1437,1453"],
              ["1_630",3,..,"1_674,1_684,1_708,1_762,1_770"],["1473",4,..,"1482,1498"]
person after: identical
```
Gotchas:
- Ids are per editing session (one doc key): Word's V8 says load ids are assigned in load order and change on reopen;
  same mechanism here (`"1428"` style counters). Durable anchors across reopen: shape names (`SetName`/`GetName`,
  saved as `p:cNvPr name`) round-trip (Q8 checks the saved file); a slide has no public name setter.
- A deleted slide's id simply stops being found: `slideById` returns null, which the script turns into a refusal.

### Q3. Reading

**Answer:** all of it is reachable; `slide/read3.js` is a working outline + view. Slide size `pres.GetWidth()` /
`GetHeight()` in EMU (`12191695 x 6858000` here). Positions/sizes: `GetPosX/GetPosY/GetWidth/GetHeight` (EMU) on every
drawing. Kind: `GetClassType()` = `shape | image | table | chart | group | oleObject`. Title: `GetPlaceholder().GetType()`
`"title"`/`"ctrTitle"`. Layout: `slide.GetLayout().GetName()`. Notes: `slide.GetNotesPage().GetBodyShapeText()`.
Fill/line and *effective* run formatting need internals (the public getters return only direct formatting):
shape `d.Shape.brush` / `d.Shape.pen.Fill` (compiled CUniFill), run `apiRun.Run.Get_CompiledPr(true)`.

Key parts of `slide/read3.js` (full file kept):
```js
function fillOf(uni) {                               // compiled fill: d.Shape.brush, or d.Shape.pen.Fill
  var f = uni && uni.fill;
  if (!f || f instanceof AscFormat.CNoFill) return "none";
  if (f instanceof AscFormat.CSolidFill) return hex(f.color.RGBA);
  return f instanceof AscFormat.CBlipFill ? "picture" : f instanceof AscFormat.CGradFill ? "gradient" : "pattern";
}
function runOf(r, href) {                            // r: ApiRun
  var text = String(r.GetText()).replace(/[\r\n\t]+$/, "");
  if (!text) return null;
  var cp = r.Run.Get_CompiledPr(true), out = { text: text };
  if (cp.Bold) out.b = true; if (cp.Italic) out.i = true; if (cp.Underline) out.u = true;
  out.size = cp.FontSize;                            // points
  out.font = cp.RFonts.Ascii ? cp.RFonts.Ascii.Name : null;
  // Unifill (theme colours resolved) wins; cp.Color is stale black when a theme colour applies
  out.color = cp.Unifill && cp.Unifill.fill && cp.Unifill.fill.color ? hex(cp.Unifill.fill.color.RGBA) : hex({R: cp.Color.r, G: cp.Color.g, B: cp.Color.b});
  if (href) out.href = href;
  return out;
}
// paragraphs: p.GetElement(k) is "run" (ApiRun) or "hyperlink" (ApiHyperlink: GetElement(j) runs, GetLinkedText() url)
// bullets: p.Paragraph.Get_CompiledPr2(false).ParaPr.Bullet.bulletType.type: AscFormat.BULLET_TYPE_BULLET_CHAR (1, .Char) /
//          BULLET_TYPE_BULLET_AUTONUM (2, .AutoNumType); level: ParaPr.Lvl
// tables: d.Table.TableGrid (mm, x36000 = EMU), d.GetRow(r).GetCell(c).GetContent() (an ApiDocContent), cell.Cell.Get_Shd().Unifill
```
Outline (person's page, after the builds):
```
{"size":[12191695,6858000],"slides":[{"id":"1428","index":0,"title":"Quarterly review","layout":"Title and Content","shapes":2,"notes":false},
 {"id":"1473","index":2,"title":"Next quarter","layout":"Title Only","shapes":2,"notes":true},
 {"id":"1_402","index":3,"title":"Quarterly results","layout":"Blank","shapes":5,"notes":true}, ...]}
```
View of the built slide `1_630` (trimmed):
```
{"id":"1_674","kind":"shape","x":0,"y":0,"w":12191999.76,"h":1269999.72,"fill":"#14285a","line":"none","paras":[{"align":"left","runs":[]}]}
{"id":"1_684","kind":"shape",...,"fill":"none","line":"none","paras":[{"align":"left","runs":[{"text":"Quarterly ","b":true,"size":48,"font":"Arial","color":"#ffffff"},{"text":"results","i":true,"size":48,"font":"Arial","color":"#ffd700"}]}]}
{"id":"1_708",...,"paras":[{"align":"left","runs":[...,{"text":"underlined","u":true,"size":24,"font":"Calibri","color":"#c81e1e"},..,
   {"text":"a link","u":true,"size":24,"font":"Calibri","color":"#1155cc","href":"https://example.com"}]},
   {"align":"center","runs":[{"text":"Centered paragraph","size":24,"font":"Times New Roman","color":"#000000"}]},
   {"align":"left","runs":[..],"bullet":"•"},{"align":"left","runs":[..],"bullet":"–","level":1},{"align":"left","runs":[..],"numbered":true}]}
{"id":"1_762","kind":"image","x":8254999.8,"y":1650999.96,"w":2539999.8,"h":1904999.76}
{"id":"1_770","kind":"table",...,"w":4444999.2,"h":1280159.28,"cols":[1905000,1270000,1270000],"rows":[[{"paras":[..{"text":"Region","b":true,..,"color":"#ffffff"}],"fill":"#14285a"},..]]}
notes: "Say the numbers slowly."
```
Fixture slide `1428` (placeholders): `{"id":"1437","kind":"shape","placeholder":"title",..,"runs":[{"text":"Quarterly review","size":44,"font":"Calibri","color":"#000000"}]}`,
`{"id":"1453","placeholder":"unknown",..,"bullet":"•"}` (python-pptx's `idx=1` body placeholder has no type: `"unknown"`).

Gotchas:
- **A command's return value may nest at most 10 levels** (`Asc.checkReturnCommand`); a view (shapes > table > rows >
  cells > paras > runs > run) is deeper and comes back `undefined` with no error. Return `JSON.stringify(view)` and
  parse it in runtime-office.
- **`Asc` is shadowed inside a command**: the connector wrapper declares `var Asc = {}` (`Object.keys(Asc)` =
  `["scope"]`), so `Asc.linerule_AtLeast`, `Asc.c_oAscFill`, `Asc.editor` are undefined. Use `AscFormat.*` classes
  (`instanceof AscFormat.CSolidFill`) or literals. `AscFormat.FILL_TYPE_*` are not exported (undefined).
- `ApiRun.GetColor()/GetBold()...` return only direct formatting (null when inherited): use `Get_CompiledPr(true)`.
  With a theme colour, `cp.Color` stays `{0,0,0}` while `cp.Unifill.fill.color.RGBA` is the resolved colour
  (accent2 run: Unifill `{R:192,G:80,B:77}`, Color `{r:0,g:0,b:0}`).
- `ApiParagraph.GetText()` of a table cell's paragraph ends in `"\t"`, not `"\r\n"`: strip trailing whitespace.
- A cell with no fill reads `Get_Shd()` as `#ffffff` (compiled default), not "none": read `cell.Cell.Pr.Shd` for direct fill if that matters.
- Sizes read in the same command that changed them are stale (the frame recalculates after the command): the agent's
  `GetWidth()` of a re-gridded table was still `8127796` in that command, `4444999` on the next read.

### Q4. Building a slide

**Answer:** every piece works through the Builder API plus four internal calls (hyperlink run, shape style, table
widths/heights, paragraph level). `slide/build3.js` is the tested builder (full file kept); run inside a
`GroupActions` group it produced slide `1_630` (and `1_402` in place), rendered identically in the person's editor
(`slide/shot-build4-person.png`, `slide/shot-replace-person.png`) and read back exactly (Q3 view above).

(The table frame-size fix of Q8 is folded into the code below and was re-run: `spPr.xfrm` ext `4445000 x 1143000`, grid `[1905000,1270000,1270000]`.)

Units on the 1920x1080 canvas of a 12192000x6858000 deck: **1 px = 6350 EMU = 0.5 pt = 10 twips = 0.17639 mm**;
so a font of N px is `SetFontSize(N)` (half-points), a CSS line-height L px exact is `SetSpacingLine(L*10, "exact")`.
(Checked: a 32 px Arial paragraph with `SetSpacingLine(500, "exact")` laid out two lines of `[50, 50]` px, read
from `Paragraph.Lines[i].Bottom - Top` in the next command.)
(Note for the brief: on this canvas 32 px is **16 pt**, not 24 pt; 24 pt is 48 px.)

The exact calls (from `slide/build3.js`, as run):
```js
// a slide with no placeholders, after slide `after`
var blank = pres.GetMaster(0).GetLayoutByType("blank");          // null-safe: CreateSlide's slide has no placeholders anyway
var slide = Api.CreateSlide();                                    // layout[0] ("Title Slide"), zero drawings, notes page made
if (blank) slide.ApplyLayout(blank);                              // adds no placeholders
slide.RemoveAllObjects();
pres.AddSlide(slide, after.GetSlideIndex() + 1);                  // AddSlide(slide, index): inserts at index (end when out of range)
if (scope.background) slide.SetBackground(solid(scope.background));
// ... BUILD[kind](spec) for each shape, then:
if (!slide.AddObject(d)) throw new Error("AddObject refused shape " + i);   // z-order = add order
if (scope.notes) slide.AddNotesText(scope.notes);
```
```js
  function rgb(hex) { var n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function solid(hex) { var c = rgb(hex); return Api.CreateSolidFill(Api.CreateRGBColor(c[0], c[1], c[2])); }
  function noLine() { return Api.CreateStroke(0, Api.CreateNoFill()); }
  function styleRun(run, r) {
    if (r.b) run.SetBold(true);
    if (r.i) run.SetItalic(true);
    if (r.u) run.SetUnderline(true);
    if (r.size) run.SetFontSize(Math.round(r.size * 2));          // half-points
    if (r.font) run.SetFontFamily(r.font);
    var c = rgb(r.color || "#000000");
    run.SetColor(c[0], c[1], c[2]);
  }
  function paragraphOf(pp) {
    var p = Api.CreateParagraph();
    for (var k = 0; k < pp.runs.length; k++) {
      var r = pp.runs[k];
      if (r.href) {
        // SetDisplayedText throws in slides (Styles.GetDefaultHyperlink): the run goes into the link directly.
        var link = Api.CreateHyperlink(r.href, ""), inner = Api.CreateRun();
        inner.AddText(r.text);
        styleRun(inner, r);
        link.ParaHyperlink.Add_ToContent(0, inner.Run, false);
        p.AddElement(link);
      } else {
        var run = Api.CreateRun();
        run.AddText(r.text);
        styleRun(run, r);
        p.AddElement(run);
      }
    }
    p.SetJc(pp.align || "left");
    if (pp.line) p.SetSpacingLine(Math.round(pp.line * 240), "auto");   // multiple of single
    p.SetSpacingBefore(Math.round((pp.before || 0) * 20));               // points -> twips
    p.SetSpacingAfter(Math.round((pp.after || 0) * 20));
    if (pp.bullet) p.SetBullet(Api.CreateBullet(pp.bullet));
    else if (pp.numbered) p.SetBullet(Api.CreateNumbering("ArabicPeriod", pp.start || 1));
    else p.SetBullet(Api.CreateBullet(""));                               // explicitly none
    if (pp.indent !== undefined) { p.SetIndLeft(pp.indent); p.SetIndFirstLine(pp.hanging ? -pp.hanging : 0); }
    // Last: every ApiParagraph setter writes its whole ParaPr back, which has no level.
    if (pp.level) p.Paragraph.Set_PresentationLevel(pp.level);
    return p;
  }
  function fillContent(dc, paras) {
    dc.RemoveAllElements();                      // leaves one empty paragraph
    for (var i = 0; i < paras.length; i++) dc.Push(paragraphOf(paras[i]));
    dc.RemoveElement(0);
  }
  function plainTableStyle() {
    var styles = pres.Presentation.globalTableStyles.Style;
    for (var id in styles) if (styles[id].Name === "No Style, No Grid") return id;
    return pres.Presentation.DefaultTableStyleId;
  }
  var BUILD = {
    rect: function (s) {
      var sh = Api.CreateShape("rect", s.w, s.h, solid(s.fill), noLine());
      sh.Shape.setStyle(null);
      sh.SetPosition(s.x, s.y);
      return sh;
    },
    text: function (s) {
      var sh = Api.CreateShape("rect", s.w, s.h, Api.CreateNoFill(), noLine());
      sh.Shape.setStyle(null);
      var dc = sh.GetDocContent();              // creates the txBody: paddings and anchor need it
      sh.SetPaddings(0, 0, 0, 0);
      sh.SetVerticalTextAlign("top");
      fillContent(dc, s.paras);
      sh.SetPosition(s.x, s.y);
      return sh;
    },
    image: function (s) {
      var im = Api.CreateImage(s.src, s.w, s.h);
      im.SetPosition(s.x, s.y);
      return im;
    },
    table: function (s) {
      var t = Api.CreateTable(s.cols.length, s.rows.length), T = t.Table, mm = s.cols.map(function (w) { return w / 36000; });
      T.Set_TableStyle(plainTableStyle());       // first: it clears direct formatting
      t.SetTableLook(false, false, false, false, false, false);
      t.SetPosition(s.x, s.y);
      // Column widths: the grid, and each cell's own width (CreateTable gives every cell an equal one, which wins).
      T.SetTableGrid(mm);
      for (var r = 0; r < s.rows.length; r++) {
        var row = t.GetRow(r);
        // A slide table's row height is the whole row (a:tr h): ApiTableRow.SetHeight takes the margins off it.
        row.Row.Set_Height(s.rows[r].h / 36000, 0);  // 0 = linerule_AtLeast (Asc is shadowed in a command)
        for (var c = 0; c < s.rows[r].cells.length; c++) {
          var spec = s.rows[r].cells[c], cell = row.GetCell(c), pr = cell.Cell.Pr.Copy();
          pr.TableCellW.W = mm[c];
          cell.Cell.Set_Pr(pr);
          // No fill must be said: unshaded cells take the table style's white (wholeTbl lt1).
          if (spec.fill) { var f = rgb(spec.fill); cell.SetShd("clear", f[0], f[1], f[2]); } else cell.SetShd("nil");
          if (spec.border) {                    // canvas px -> mm (1 px = 6350 EMU, 36000 EMU = 1 mm)
            var bw = spec.border.w * 6350 / 36000, bf = solid(spec.border.color);
            cell.SetCellBorderTop(bw, bf); cell.SetCellBorderBottom(bw, bf); cell.SetCellBorderLeft(bw, bf); cell.SetCellBorderRight(bw, bf);
          }
          if (spec.pad) {                       // canvas px -> twips (1 px = 0.5 pt = 10 twips)
            cell.SetCellMarginTop(spec.pad[0] * 10); cell.SetCellMarginRight(spec.pad[1] * 10);
            cell.SetCellMarginBottom(spec.pad[2] * 10); cell.SetCellMarginLeft(spec.pad[3] * 10);
          }
          fillContent(cell.GetContent(), spec.paras);
        }
      }
      // The saved frame size (p:xfrm) is not recalculated from the grid: set it, or it stays CreateTable's width.
      var sum = function (a) { return a.reduce(function (x, y) { return x + y; }, 0); };
      t.Drawing.spPr.xfrm.setExtX(sum(s.cols) / 36000);
      t.Drawing.spPr.xfrm.setExtY(sum(s.rows.map(function (row) { return row.h; })) / 36000);
      return t;
    },
  };
```
The spec it ran (`slide/spec4.py`, EMU = px*6350): a 1920x200 `#14285A` rect; a title text box (48 half-points Arial,
white bold + gold italic); a 1000x500 text box with mixed runs, a link, centre/right paragraphs, `•` level 0, `–`
level 1, two numbered items; a 400x300 PNG data URL; a 300/200/200 x 60/120 px table with header fill, 2 px borders,
8/12 px padding; notes. Result: `{"slide":"1_630","index":1,"ids":["1_674","1_684","1_708","1_762","1_770"]}`.
Read back on the person's page: table `cols:[1905000,1270000,1270000]` (= 300/200/200 px), row heights set `[60,120]`
px; paragraph `"level":1`, `"bullet":"•"`, `"numbered":true`, `"href":"https://example.com"`, notes text, rect
`"fill":"#14285a","line":"none"`, text boxes `"fill":"none","line":"none"`.

Gotchas (each one hit and fixed in this spike):
- **`Api.CreateShape` attaches a theme shape style** (`fontRef` lt1 = white text, `effectRef idx 2` = the theme's
  shadow, drawn as a grey line under the rect). Fix: `shape.Shape.setStyle(null)` (internal, history-tracked; the
  person's render shows no shadow and black default text). Also set every run's colour explicitly.
- **`Api.CreateShape` has no text body until `GetDocContent()` is called**: `SetPaddings` / `SetVerticalTextAlign`
  before that are silent no-ops (insets stayed 2.54/1.27 mm; after `GetDocContent()` first, `lIns = 0`).
  Default after that: anchor top (4), `wrap: square`, `textFit: null` (no autofit) — nothing to switch off.
- `dc.RemoveAllElements()` leaves one empty paragraph: push the new ones, then `dc.RemoveElement(0)`.
- **`ApiHyperlink.SetDisplayedText` throws in the slide editor** (`Styles.GetDefaultHyperlink is not a function`);
  `Api.CreateHyperlink(url, tip)` takes no text. Fix: `link.ParaHyperlink.Add_ToContent(0, apiRun.Run, false)` then
  `paragraph.AddElement(link)` (internal). Reads back as an `ApiHyperlink` with `GetLinkedText()` = the url.
- **Paragraph level**: `ApiParagraph.SetOutlineLvl` is Word's outline level, not pptx `lvl`. `p.Paragraph.Set_PresentationLevel(n)`
  (internal, history-tracked) sets it, and must be called **after** every `ApiParagraph` setter: each setter writes
  its whole cached `ParaPr` back, which drops `Lvl` (first try read back no level).
- Bullets: `p.SetBullet(Api.CreateBullet("•"))`, numbers `p.SetBullet(Api.CreateNumbering("ArabicPeriod", start))`,
  none `p.SetBullet(Api.CreateBullet(""))`; hanging indent `SetIndLeft(twips)` + `SetIndFirstLine(-twips)`.
- Units of Builder setters: `SetFontSize` half-points; `SetSpacingBefore/After` twips; `SetSpacingLine(n, "auto")` n/240
  = multiple, `"exact"/"atLeast"` twips; `SetIndLeft` twips; `SetCellMargin*` twips; `SetCellBorder*(size, fill)` size
  in **mm**; `Api.CreateShape/CreateImage/SetPosition/SetSize` EMU.
- **Tables**: `Api.CreateTable` gives the default style ("Medium Style 2 - Accent 1") and equal **cell widths that win
  over the grid**. Fix (internal): `T.Set_TableStyle(<id of "No Style, No Grid">)` first (it clears direct
  formatting), `T.SetTableGrid(mm[])` **and** each cell's `Pr.TableCellW.W = mm` via `Cell.Set_Pr(copy)`. Without the
  cell widths the frame stayed 8127796 EMU (1280 px) wide; with them 4444999 (700 px) as asked.
- **Row height**: `ApiTableRow.SetHeight` subtracts margins and borders (Word semantics), but a slide row's height is
  the whole row: asked 120 px, got 103.6 px. Fix: `row.Row.Set_Height(emu / 36000, 0)` (0 = at least): 80 px asked,
  80 px real. A row still grows to fit its content: asked 60 px, got 81.6 px = a 57.6 px line (48 px Calibri) +
  2 x 12 px padding. Fine when the padding and font come from Chromium's own measurement of that row.
- **An unfilled cell renders white**, not transparent ("No Style, No Grid" has a `lt1` whole-table fill): say
  `cell.SetShd("nil")` (pixel went from `(255,255,255)` to the slide's `(255,248,231)`).
- `Api.CreateShape/CreateTable/CreateImage` parent the object to the **current** slide (`private_GetCurrentSlide()`);
  `CreateTable` returns null if there is none. `AddObject` re-parents, and `AddSlide` makes the new slide current.
- An exception mid-build left a half-built slide **in both editors** (first run of the hyperlink bug: slide `1_295`
  with two shapes reached the person): always run the build inside a group (Q7).
- `Api.CreateImage(dataUrl, w, h)` works; the person's editor reads the picture as a media file name
  (`88bc19bb…_image1.png`): the data URL is uploaded to the document server, not kept inline.

### Q6. Replace, order, remove

**Answer:** replace **in place keeps the slide id**: `RemoveAllObjects()` + `ApplyLayout(blank)` + clear the notes
body + rebuild (`scope.replace` in `slide/build3.js`). A new slide gets a fresh id `"<editor index>_<n>"` (agent `1_…`).
Order: for each target position `i`, `slideById(ids[i]).MoveTo(i)` when it is not there yet. Remove: `slide.Delete()`
(= `pres.RemoveSlides(index, 1)`).

Replace (`slide/spec5.py 1_402`, in a committed group):
```js
slide = slideById(scope.replace);
slide.RemoveAllObjects();
if (blank) slide.ApplyLayout(blank);
var notesPage = slide.GetNotesPage();
if (notesPage && notesPage.GetBodyShape()) notesPage.GetBodyShape().GetDocContent().RemoveAllElements();
// ... same BUILD loop, slide.SetBackground(solid("#FFF8E7")), slide.AddNotesText("Replaced notes.")
```
```
{"slide":"1_402","index":1,"ids":["1_861","1_871","1_894"]}
person: ["2_19",0,..],["1_402",1,"Blank","1_861,1_871,1_894"],["1428",2,..] ...   view notes: "Replaced notes."
```
Order + remove (`slide/order.js`, one committed group):
```js
for (var r = 0; r < scope.remove.length; r++) { var gone = slideById(scope.remove[r]); if (!gone) return { refused: ... }; gone.Delete(); }
for (var i = 0; i < scope.order.length; i++) { var s = slideById(scope.order[i]); if (!s) return { refused: ... }; if (s.GetSlideIndex() !== i) s.MoveTo(i); }
```
```
before: ["2_19","1_402","1428","1_630","1473"]   scope {"remove":["2_19"],"order":["1473","1428","1_402","1_630"]}
agent:  {"now":["1473","1428","1_402","1_630"]}
person: ["1473",0,..],["1428",1,..],["1_402",2,..,"1_861,1_871,1_894"],["1_630",3,..]
```
Gotchas: `ApiSlide.Duplicate(pos)` exists (copy with new ids). `RemoveSlides()` with no args removes **all** slides.
`AddSlide`/`RemoveSlides` move the agent editor's current slide (`CurPage`); harmless, the agent has no screen.

### Q7. All or nothing

**Answer: yes, in both directions.** `StartAction ["GroupActions"]` holds every change of every command until the group
ends: the person sees nothing meanwhile. `EndAction ["GroupActions", "", "cancel"]` took back a new slide, a slide
deletion, a reorder and an in-place replace (three commands) in the agent's editor, and the person never saw any of it;
the person's concurrent edit was kept, and both editors converged. A committed group reached the person about 400 ms
after `EndAction` returned.

Cancel run (shell, both pages on one key; `snap` = `read3.js {"action":"outline"}`):
```
A0 = U0: 1473 "Next quarter" | 1428 "Quarterly review" | 1_402 "Replaced in place" (3) | 1_630 "Quarterly results" (5)
method StartAction ["GroupActions"]
cmd build3.js (spec4, after 1473)                 -> {"slide":"1_929","index":1,...}
cmd order.js {"remove":["1428"],"order":["1_630"]} -> {"now":["1_630","1473","1_929","1_402"]}
cmd build3.js (spec5, replace 1_402)              -> {"slide":"1_402","index":3,"ids":["1_1160","1_1170","1_1193"]}
person: title of slide 1473 += " (person)"        -> "Next quarter (person)"
A-during: 1_630 | 1473 "Next quarter" | 1_929 | 1_402        (agent does not see the person's edit yet)
U-during: 1473 "Next quarter (person)" | 1428 | 1_402 | 1_630 (person sees none of the agent's)
method EndAction ["GroupActions","","cancel"]
A-after = U-after: 1473 "Next quarter (person)" | 1428 | 1_402 (shapes 1_861,1_871,1_894, notes "Replaced notes.") | 1_630
```
Commit run (Q4's first group): person during the group `["1428",..],["1_43",..],["1473",..]`; `EndAction
["GroupActions",""]`; person 405 ms later: `...,["1_402",3,"Blank","1_446,1_456,1_480,1_534,1_542"]`.

Gotchas:
- Same as Word: the agent's page defers the person's changes while the group is open; keep groups short.
- Without a group, a command that throws half-way leaves its first changes in both editors (Q4, slide `1_295`).
- `method CanUndo` answers `undefined` in the slide editor (no value), unlike Word: do not rely on it.
- A data-URL picture is uploaded when `CreateImage` runs; a cancel removes the shape, the uploaded media stays on the
  document server unreferenced (harmless).

### Q8. Flush

**Answer:** the host page's `onDocumentStateChange(false)` that follows the commit is the reliable signal: after
`EndAction ["GroupActions", ""]` the editor fires `true … true … false` 11–92 ms later (6 runs), and the `false`
means sent and acknowledged. **The `isDocumentModified()` flag is NOT reliable right after a commit**: it reads
`false` before the changes are even sent (6/6 runs), so runtime-office's current `flushed()` pre-check
(`value === false ? true : waitFlushed(...)`) would report "flushed" too early for a grouped slide edit. From a
command the flag is `editor.isDocumentModified()` (`editor` is a reachable global, `=== Api.GetPresentation().Presentation.Api`).
A forcesave after the `false` event returns `error 0` and a status-6 callback (4.2 s on DEV) whose `.pptx` holds the change.

Timing (host page script `slide/race2.js`: group, one edit, `EndAction`, then the flag, then wait):
```js
await window.runMethod("StartAction", ["GroupActions"], 10000);
await window.runCommand(wrap(`function () { ... AddText("."); return 1; }`), 10000);
const t0 = Date.now();
await window.runMethod("EndAction", ["GroupActions", ""], 10000);
const m = (await window.runCommand(wrap(`function () { return editor.isDocumentModified(); }`), 10000)).value;
```
```
{'noop': False, 'endActionMs': 1, 'flagAfter': False, 'events': [[18, True], [25, True], [92, True], [92, False]]}
{'noop': False, 'endActionMs': 0, 'flagAfter': False, 'events': [[11, True], [11, True], [70, True], [70, False]]}
{'noop': False, 'endActionMs': 1, 'flagAfter': False, 'events': [[40, True], [40, True], [49, True], [49, False]]}
{'noop': False, 'endActionMs': 2, 'flagAfter': False, 'events': [[37, True], [37, True], [48, True], [48, False]]}
{'noop': False, 'endActionMs': 1, 'flagAfter': False, 'events': [[32, True], [32, True], [42, True], [45, False]]}
{'noop': True,  'endActionMs': 0, 'flagAfter': False, 'events': [[18, True], [18, True], [25, True], [25, False]]}
```
`slide/race.js` (same, one run): `in-group modified false @37ms`, `EndAction returned @38`, `modified right after
EndAction false @49`, events `true @69`, `false @79`. `slide/race3.js`: a read-only command fires **no** event; an
**empty** group (`StartAction` + `EndAction`, nothing between) fires `true,true,true,false` (13–26 ms). So every
committed group produces a `false` to wait for; wait for the first `false` with `t >= ` the `EndAction` send time.

Forcesave (`slide/flushrun.sh`, `slide/forcesave.py`: HS256 JWT in the body, secret read from
`config.dev.yaml` into a variable, never printed):
```
cmd (in group): AddText(" — saved") on the title  -> {"text":"Quarterly review — saved\r\n","modified":false}
EndAction; events +137 true, +137 true, +147 true, +149 false
/command {"c":"forcesave","key":…,"userdata":"{\"op\":\"slide-flush\",\"n\":1}"} -> {"key":"0091875e…","error":0}
status 6 after 4165 ms: {"key":"0091875e…","status":6,"users":["agent:spike"],"userdata":"{\"op\":\"slide-flush\",\"n\":1}",
                         "forcesavetype":0,"filetype":"pptx"}
```
`slide/deckdump.py saved/0091875e…-5.pptx` (python-pptx):
```
size 12191695 6858000 slides 4
--- slide 0 sldId=256 layout='Title Only' notes='Mention the lease.'      Title 1 text='Next quarter (person)'
--- slide 1 sldId=257 layout='Title and Content' notes=''               Title 1 text='Quarterly review — saved'
--- slide 2 sldId=258 layout='Blank' notes='Replaced notes.'            AUTO_SHAPE x2, TABLE cols [2539999, 1904999] rows [508000]
--- slide 3 sldId=259 layout='Blank' notes='Say the numbers slowly.'    AUTO_SHAPE x3, PICTURE, TABLE cols [1904999,1269999,1269999] rows [381000,762000]
```
Second save after naming (`-6.pptx`, unzipped `ppt/slides/slide4.xml`):
```
--- slide 3 sldId=259 layout='Blank' name='agent:slide-results'
  'agent:banner' AUTO_SHAPE x=0 y=0 w=12191999 h=1269999
  'agent:table' TABLE x=7302499 y=3936999 w=4444999 h=1143000   cols [1904999,1269999,1269999] rows [381000,762000]
<p:cSld name="agent:slide-results">
<a:bodyPr ... wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" ... anchor="t" ...></a:bodyPr>   (no autofit element)
<a:hlinkClick r:id="rId3" tooltip="">
<a:pPr marL="685800" lvl="1" indent="-228600" algn="l">
p:style count: 0
```
Gotchas:
- **Durable anchors across reopen**: saved `sldId` (256…) and shape `cNvPr id` (random) are regenerated on every save.
  What round-trips: shape names (`SetName`, public) and the slide's `cSld name` (internal
  `slide.Slide.setCSldName(name)`, history-tracked; the person's editor saw it at once). Builder shapes are saved
  with `name=""` unless named.
- **Table frame size is saved stale**: `p:graphicFrame/p:xfrm/a:ext` keeps CreateTable's 8127796 EMU width (the
  in-memory `extX` is right, `spPr.xfrm` is not). Fix: `t.Drawing.spPr.xfrm.setExtX(sumColsEmu / 36000)` and
  `setExtY(sumRowsEmu / 36000)` (then the saved ext was 4444999 x 1143000). `recalculateSizes()` does not touch xfrm.
- **DEV also saves on a timer**: two status-6 callbacks with `forcesavetype: 2` and no `userdata` arrived unasked
  (about every 6 minutes while edited). The runtime must match a save to its forcesave by `userdata`.
- A forcesave sent before the flush answers `error 4` (Word's V4); waiting for the post-commit `false` avoids it.

### Q9. Two writers

**Answer: there are no locks in fast co-editing (the runtime's mode), so nothing refuses the agent.** While the
person had a shape's text in edit (cursor in it, typing), and even while the person held an un-sent edit to that
shape in an open group, every lock read `kLockTypeNone` in both editors, and the agent's in-place replace and its
deletion of that slide both applied. Outcome: the agent's structural change wins; the person's editor silently drops
its selection (next keystroke goes nowhere, no error), and a held edit to a deleted shape is lost on commit; both
editors converge. How the agent would see a lock: `drawing.Drawing.Lock.Get_Type()` (shape),
`slide.Slide.deleteLock.Lock.Get_Type()` / `slide.Slide.Lock.Get_Type()` (slide); `AscCommon.c_oAscLockTypes` =
`{kLockTypeNone:1, kLockTypeMine:2, kLockTypeOther:3, kLockTypeOther2:4, kLockTypeOther3:5}`. They stayed 1 throughout.

Person in text edit (person page): command `Api.GetByInternalId("1_871").Select(); run.MoveCursorToPos(4)`, then frame
`editor.WordControl.m_oLogicDocument.EnterText([0x5A])`:
```
frame: {"inText":true,"sel":["1_871"]}            text on both pages: "ReplZaced in place"
agent locks (slide/locks.js): {'id':'1_402','slideLock':1,'deleteLock':1,'shapes':[['1_861',1,None],['1_871',1,None],['1_894',1,None]]}
person locks: identical
```
Lock watched every 20 ms around a keystroke in the person's editor, and an explicit
`P.Document_Is_SelectionLocked(AscCommon.changestype_Drawing_Props)` on the selected shape:
```
[["before",0,1,1],["after EnterText",9,1,1],["t",29,1,1]]                  (shape lock, slide deleteLock)
[["sel","1_708",1],["locked?",false,1,true,1],["200ms",1]]                  (Is_Fast() = true)
```
Agent replaces slide 1_402 in place while the person's cursor is in shape 1_871:
```
person before: {"sel":["1_871"],"inText":true}
agent build3.js replace -> {"slide":"1_402","index":2,"ids":["1_1250","1_1260","1_1283"]}; EndAction ok
person after: list shows 1_402 with 1_1250,1_1260,1_1283; frame {"before":{"sel":[],"inText":false},"err":null,"after":{"sel":[]}}
person host: dead null, no new onDocumentStateChange (the keystroke changed nothing)
```
Person holds an edit (open group: title of shape 1_1260 += " (person, held)"); agent deletes slide 1_402:
```
agent order.js {"remove":["1_402"]} -> {"now":["1473","1428","1_630"]}  (no refusal; locks all 1 on both pages)
person (group still open) still lists 1_402; person EndAction commit
agent = person: ["1473",0,..],["1428",1,..],["1_630",2,..]   both pages dead: null
```
Gotchas:
- Strict co-editing mode would take real locks; the runtime's config pins fast mode (`coEditing: {mode:"fast", change:false}`), so
  the design can treat slides as lock-free and protect the person by other means (e.g. refuse to replace/delete a
  slide whose content hash changed since the agent read it, as word.js does for blocks).

### Q10. SVG

**Answer: the editor takes an SVG directly.** `Api.CreateImage("data:image/svg+xml;base64,…", w, h)` uploads it
(`…_image1.svg`), both editors render it as vector (`slide/shot-svg-person.png`), and the saved `.pptx` stores it the
PowerPoint 2016+ way: an `asvg:svgBlip` (the SVG, `media1.svg`) inside a `a:blip` whose main image is a PNG fallback
the server rasterized **at the SVG's intrinsic size** (400x300 here, while it is shown at 800x600 px).

Run (`slide/spec-svg.json` through `build3.js`, image kind = `Api.CreateImage(s.src, s.w, s.h)` + `SetPosition`):
```
svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300"><rect .. fill="#eef"/>
       <circle cx="200" cy="150" r="110" fill="#c81e1e"/><text .. font-family="Arial">SVG</text></svg>'
-> {"slide":"1_1318","index":1,"ids":["1_1362","1_1370"]}
agent and person: {"kind":"drawing","src":"3916200b7e012583e88c38ef31c562f6_image1.svg"}
saved -8.pptx slide2.xml:
<a:blip r:embed="rId3"><a:extLst><a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}"><asvg:svgBlip ... r:embed="rId4">
rels: rId3 -> ../media/image1.png (PNG 400 x 300 RGBA), rId4 -> ../media/media1.svg
```
Gotchas:
- An SVG picture's `GetClassType()` is **`"drawing"`**, not `"image"` (a PNG picture is `"image"`): the reader must
  treat both as pictures.
- The fallback PNG is low-resolution when the SVG's `width`/`height` are smaller than its box; set them to the box
  size (or a multiple) if old viewers matter. Text inside the SVG depends on the viewer's fonts, and the editor's SVG
  renderer is not Chromium's: rasterizing in Chromium stays the safe default for complex SVG (filters, CSS,
  `foreignObject`); a plain SVG can go in as-is.

### Q5. Text fidelity and fonts

**Answer: the editor's text layout equals Chromium's to 0.01 px, provided Chromium (a) has the same font files,
(b) runs with `font-kerning: none; font-variant-ligatures: none` (the editor applies neither), (c) for @font-face
fonts also `text-rendering: geometricPrecision` (else Chromium rounds web-font advances to whole px), and (d) uses
`line-height: 1.2` (the editor's single spacing is 1.2 x the font size: 38.4 px lines at 32 px).** With that, the
40-word paragraph broke into the same lines, word for word, in every font tried. The document server has **161
fonts** (list below), including real Microsoft Calibri/Arial/Times New Roman; its font files are served at
`<ds>/<version>/fonts/<id>` (ids from `sdkjs/common/AllFonts.js`), each a TTF whose **first 32 bytes are XORed with
the 16-byte key `A0 66 D6 20 14 96 47 FA 95 69 B8 50 B0 41 49 48`** (decoded, `fc-query` reads them). This
workstation's Chromium has Arial, Times New Roman, Carlito, Liberation Sans, Verdana, Georgia, DejaVu Sans, Noto
Sans by name (and "Calibri" through fontconfig's Carlito alias), but **not** Open Sans, Montserrat, Ubuntu, etc.

Mapping: the canvas's 1000 px box is `1000*6350` EMU and a 32 px font is `SetFontSize(32)` (16 pt); the ratio is what
matters (the brief's "32 px (24 pt)" is the 96-dpi reading; on this canvas 24 pt would be 48 px).

Editor side (`slide/fid-build.js` + `slide/fid-measure.js`, in a group, then cancelled): one text box per font, no
style, zero insets, top anchor, one run of the 40 words; then, in the next command (after the recalc):
```js
var P = sh.GetDocContent().GetElement(0).Paragraph;               // internal Paragraph
for (var l = 0; l < P.Lines.length; l++) starts.push(run.protected_GetRangeStartPos(l - run.StartLine, 0));
out.push({ font, lines: P.Lines.length, wordsPerLine, contentHeightPx: sh.Shape.txBody.content.GetSummaryHeight() * 36000 / 6350 });
// one-line width: P.Lines[0].Ranges[0].W (mm) * 36000 / 6350 = canvas px
```
Chromium side (`slide/chromium-lines.mjs`, headless chromium-1223 via runtime-office's playwright-core): a
`width:1000px;font-size:32px;line-height:1.2` div, one `<span>` per word, lines = distinct span tops.

| font | editor lines / words per line | Chromium, system font, defaults | Chromium, kerning+ligatures off | Chromium, server's own file via @font-face (+geometricPrecision) |
|---|---|---|---|---|
| Arial | 4 [10,13,12,5] | same | same | same |
| Times New Roman | 4 [11,14,13,2] | same | same | same |
| Carlito | 4 [11,13,13,3] | same | same | — |
| Calibri | 4 [11,13,13,3] | same (fontconfig: Carlito) | — | same |
| Liberation Sans | 4 [10,13,12,5] | same | same | — |
| Verdana | 5 [8,11,10,9,2] | same | same | — |
| Georgia | 4 [10,13,12,5] | same | same | — |
| Noto Sans | 4 [9,11,11,9] | same | same | — |
| **DejaVu Sans** | 5 [8,11,10,9,2] | **4 [8,11,11,10]** (ligatures) | 5 [8,11,10,9,2] | — |
| **Open Sans** | 4 [9,12,12,7] | **4 [11,14,13,2]** (font missing: fallback) | — | 4 [9,12,12,7] |
Heights: editor 153.6 px for 4 lines (192 for 5); Chromium 153.56 (191.95).

One-line widths of `"AVATAR WAVY TOWEL YAWN Toffee affluent office"` at 32 px (`slide/fid-width.js`,
`slide/chromium-width.mjs`, `slide/chromium-fontface2.mjs`):
```
                 editor    Chromium default  kerning:none  ligatures:none  both off   @font-face server file, both off + geometricPrecision
Arial            757.56    731.58            757.56        731.58          757.56     757.56
Times New Roman  751.71    713.73            751.72        713.73          751.72
Carlito          667.88    645.23            664.97        647.22          667.89     667.89
DejaVu Sans      809.91    785.94            808.47        787.39          809.92
Georgia          756.12    756.13            756.13        756.13          756.13
Calibri          667.88                                                    667.89 (alias)  667.89
Open Sans        745.60                                                    751.72 (missing) 745.61
Liberation Sans  757.56                                                                    757.56
(@font-face without geometricPrecision: Calibri 671, Open Sans 746, Liberation Sans 758: whole-px advances)
```
Font list (frame `AscFonts.g_font_infos.map(f => f.Name)`, 161 names; files `AscFonts.g_font_files`, 264): Abyssinica SIL,
Andale Mono, Ani, AR PL UKai CN/HK/TW/TW MBE, Arial, Arial Black, Asana, Asana Math, Caladea, Calibri, Cambria,
Candara, Cantarell, Carlito, Comic Sans MS, Consolas, Constantia, Corbel, Courier New, DejaVu Math TeX Gyre, DejaVu Sans
(+Condensed, Light, Mono), DejaVu Serif (+Condensed), Droid Sans Fallback, FreeMono, FreeSans, FreeSerif, Georgia,
Impact, Jamrul, Kacst* (15), Khmer OS (+System), Liberation Mono/Sans/Sans Narrow/Serif, Likhan, Lohit * (12), Mitra
Mono, Montserrat (+Black, ExtraBold, ExtraLight, Light, Medium, SemiBold, Thin), mry_KacstQurn, Mukti Narrow,
NanumBarunGothic, NanumGothic, NanumMyeongjo, NanumSquare (+Bold), Noto Emoji, Noto Naskh Arabic (+UI), Noto Sans
(+30 scripts, KR weights), Noto Serif Sinhala, Open Sans (+Extrabold, Light, Semibold), OpenSymbol, Padauk (+Book),
padmaa, Pothana2000, Rekha, Samyak *, Symbola, Tahoma, TakaoExGothic, TakaoGothic, TakaoPGothic, Tibetan Machine Uni,
Times New Roman, Trebuchet MS, Ubuntu (+Condensed, Light, Mono), Vemana2000, Verdana, Webdings, WenQuanYi Zen Hei
(+Mono, Sharp), ASCW3. Over HTTP: `curl <ds>/9.3.5-dev001/sdkjs/common/AllFonts.js` gives `__fonts_files` ("000"…) and
`__fonts_infos` entries like `"Open Sans",233,0,-1,-1,230,0,-1,-1` (regular file 233, bold 230, no italics).
Font ids → files checked: Arial 022 (v3.06), Calibri 027 (v1.02, Microsoft's), Carlito 087, Liberation Sans 157,
Times New Roman 062, Open Sans 233; local files differ in version (local Arial v2.82) but not in advances.
De-obfuscation (Python, `slide/dsfonts/`):
```python
key = bytes([0xA0,0x66,0xD6,0x20,0x14,0x96,0x47,0xfa,0x95,0x69,0xB8,0x50,0xB0,0x41,0x49,0x48])
d = bytearray(open("022", "rb").read())
for k in range(32): d[k] ^= key[k % 16]          # -> starts 00010000; fc-query: "Arial | Regular | v200540"
```
Gotchas:
- A font the server lacks: Chromium and the editor fall back differently (Open Sans row). runtime-office should
  measure only with fonts on the server's list, loaded from the server itself (or baked into its image from the same
  files), and map anything else to a listed font before measuring.
- The editor's line height ignores the font's own ascent/descent (38.4 px for every font at 32 px); Chromium's
  `line-height: normal` does not: always set an explicit `line-height` in the HTML (1.2 = editor "single").
- Measurement needs the recalc that follows a command: read `Lines` in a second command.

### Open questions

- **Locks in strict mode / UI drags.** No lock could be produced in fast mode (typing, an explicit
  `Document_Is_SelectionLocked`, a held group). Whether a lock appears while a person drags or resizes a shape with
  the mouse was not tried: the probe pages have no pointer input (only `eval`). If one does, the agent would read it as
  `Drawing.Lock.Get_Type() >= 3`; whether a Builder command then refuses or throws is unknown.
- **The runtime-office pod's Chromium fonts.** Fidelity was proved on this workstation, with system fonts and with
  the document server's own files via @font-face. Which fonts the runtime-office image has was not checked; loading the
  server's files (de-obfuscated, `AllFonts.js` names) avoids depending on it. Bold/italic faces were not measured
  (only regular), nor non-Latin scripts.
- **Placeholder-based decks.** Builds used the "Blank" layout and free shapes. A user deck whose master has no
  "blank" layout falls back to `CreateSlide`'s layout 0 (still no placeholders, but that layout's own decorations
  show). Filling a user's existing title/body placeholders (keeping their theme styling) was not tried.
- **Undo for the person.** A committed group should be one Undo step for the agent (Word V5); whether the person's
  Undo can revert an agent slide build was not tried.
- **Groups and media.** The data-URL picture is uploaded during the command; how long a large image (MBs) takes
  inside a group, and whether the command waits for the upload, was not measured (the test images were 100 B–9.6 KB).
- **Charts.** `Api.CreateChart` exists in the slide API but was out of the brief's element list; not tried.

### Claude Slides' format as native slide objects (Euro-Office 9.3.5, DEV `office.densemax.local`)

Probe port 8793. Agent page `agent:spike`, person page `user:spike` ("Flavius"), one fresh key per run. Every test
builds its own slide named `t:<topic>` (internal `Slide.setCSldName`) through `slide2/run.py <page> slide2/<test>.js`,
which wraps `slide2/lib.js` + the test body in `function () { "use strict"; try { … } catch (e) { return {__error} } }`.
`lib.js` helpers (all canvas px; 1 px = 6350 EMU on the 1920x1080 canvas of a 12192000x6858000 deck):
`shape(preset, x, y, w, h, fill, stroke)` = `Api.CreateShape` + `Shape.setStyle(null)` + `SetPosition`;
`textBox(...)`; `put(slide, drawing, name)` = `SetName` + `AddObject`; `newSlide(name, bg)`.
Evidence per test: the command's return, the **person's** render (`slide2/shot2.py`, the editor's own canvas once the slide has arrived),
the saved `.pptx` (forcesave via `slide2/save.sh`, slide XML via `slide2/xml.py`), and where it matters a pixel
comparison against Chromium's render of the same CSS (`slide2/chrome-shot.mjs` + `slide2/cmp.py`: mean |diff| per
box, 0–1 = identical, resampling of hard edges gives ~5–10) and LibreOffice's render of the saved file
(`slide2/lo.sh`, a third-party reader standing in for PowerPoint). The SDK source (`sdkjs/slide/sdk-all.js`, fetched
from the document server into `slide2/sdk/`, grep with `slide2/g.sh`) was read to find each call.

#### What most affects the design

1. **Almost everything is native** (shapes, custom geometry for any corner radii/skew/double borders/routed
   connectors with heads, linear and repeating gradients incl. alpha stops, strokes, rotation, text styles, crops,
   transitions incl. **Morph**, builds), pixel-identical to Chromium where measured. **Pictures:** radial gradients,
   blurred/inset shadows and text shadows (drawn by Chromium as a shadow-only picture grouped behind the native
   shape), filters, blend modes, backdrop-filter, skewed text/pictures, fancy underlines, rotated tables.
2. **A throw inside some builder calls poisons the whole document** (registry left off → `off_` ids → the person is
   disconnected into view mode, the file never saves: status 3/7). Always build in a group, restore
   `g_oTableId.m_bTurnOff`/History in the catch, cancel, and recycle the page if the flag stays set. **Never call
   `CPresentation.addSection`** (same outcome): `data-section` cannot be native.
3. **Traps that silently diverge the person's copy or the file:** picture crop/opacity edited in place is not sent
   (build pictures whole); `SetTextFill` is not saved (use `SetFill`); `Api.RGBA` alpha is dropped (use
   `transparent`); a 0 height becomes 1 inch; negative rotations are saved negative; the same SVG twice in one command
   loses its SVG in the file; `CreateShape("line")` makes a connector that `GetAllDrawings` never returns.
4. **Names:** public `SetName` is deck-unique (it renames the other holder) and is lost on a group before
   `AddObject`; Morph's `!!id` pairs need the internal `Drawing.setName`.
5. **The editor draws less than it saves** (shadow blur, inner shadow, `cmpd`, `algn=in`, picture effects, text-box
   shadows): decide native vs picture by what the person's editor shows, not by what OOXML can say.

#### 0. Cross-cutting: a throw inside a builder call can poison the document for everyone (found on the way)

**Answer:** builder calls that run inside `AscCommon.ExecuteNoHistory` (no try/finally) leave the editor's object
registry switched off when they throw (`AscCommon.g_oTableId.m_bTurnOff = true`, History off). Every object made
after that, in this and later commands, gets an unregistered `off_…` id; the next change the agent sends puts the
**person's editor silently into view mode** (`editor.isViewMode = true`, no `onError`, commands answer nothing).
The one trigger met: `Api.CreateShape("line" | any connector preset)` while the current slide holds drawings added
earlier in the same command (it searches them for connection sites; they have no transform yet). Fix, tested:
create line/connector shapes before any `AddObject` in a command (section 2), and in the command wrapper's `catch`
restore the registry, then cancel the group:
```js
} catch (e) {
  AscCommon.g_oTableId.m_bTurnOff = false;            // what ExecuteNoHistory's tail would have done
  if (!AscCommon.History.Is_On()) AscCommon.History.TurnOn();
  return { __error: String(e && e.stack || e) };
}   // then EndAction ["GroupActions", "", "cancel"]
```
Repro (`slide2/poison.js`, `poison2.js`, then `poison3.js` + `poison4.js` on a fresh key):
```
poison.js  -> {"before":false,"err":"TypeError: Cannot read properties of null (reading 'TransformPointX')",
               "after":true,"nextId":"off_3244","historyOn":false}
poison2.js (next command, a plain rect) -> {"off":true,"historyOn":true,"id":"off_3354"}
person frame: {"viewMode":true,"pages":3}; person `cmd function(){return 1}` -> {"sentAt":…} (no value)
--- with the catch above, inside a group that is then cancelled:
poison3.js -> {"refused":"TypeError: …TransformPointX…","offWas":true,"historyWas":false,"offNow":false,"historyNow":true}
poison4.js (next command) -> {"id":"1_99","off":false}; person {"viewMode":false}, lists slide 1_55 with shape 1_99
```
**And the document is lost.** Every poisoned key (these two, and the sections one in section 10) ended, after their pages closed, with a status **3** callback (error
saving; `{"status":3,"notmodified":false,"url":false}`), and the poisoned key's timer autosave before that was status
**7** (forcesave error). The person was disconnected at the moment of poisoning (`actions [{"type":0,"userid":"user:spike"}]`).
So every edit of that session, the person's included, is never written back.

Gotcha: runtime-office should also check `AscCommon.g_oTableId.m_bTurnOff` after any command that returned an
error and recycle the page if it is still `true` (a poisoned agent page must never send another change).

#### 1. Fills

**Answer:** linear gradients (any angle, 2–8+ stops, hard stops) are native and pixel-identical to Chromium, with
one internal flag (`lin.scale = false`); a repeating gradient is native as an expanded list of hard stops; rgba and
fill opacity are native through the internal `CUniFill.transparent`; the slide background takes any of these.
**Radial gradients become pictures**: the editor cannot place the centre, draws a circle (not CSS's ellipse), and
saves `<a:path path="circle"/>` without `fillToRect`, which other readers draw from the top-left corner.

Calls (`slide2/f1.js`, `slide2/f2.js`, verbatim):
```js
function stops(list) { return list.map(function (st) { return Api.CreateGradientStop(col(st[0]), Math.round(st[1] * 1000)); }); }  // pos: 1/1000 %
function linear(list, cssDeg, scaled) {
  var f = Api.CreateLinearGradientFill(stops(list), (((cssDeg - 90) % 360 + 360) % 360) * 60000);   // angle: 1/60000 deg
  f.UniFill.fill.lin.scale = !!scaled;        // internal; the builder leaves scaled="1"
  return f;
}
shape("rect", 40, 300, 400, 200, linear([["#000000", 0], ["#00C000", 30], ["#FFFFFF", 100]], 60, false));
var tr = solid("#FF0000"); tr.UniFill.transparent = 128;          // internal: 0..255 opacity of the fill
put(s, shape("rect", 1360, 300, 400, 200, tr), "fillTransparent");
textBox(40, 560, 600, 120, "Gradient behind text", 64, "#FFFFFF", <a linear fill>);   // text box background = its fill
s.SetBackground(linear([["#FFFFFF", 0], ["#DDE6F5", 100]], 160, false));             // slide background
// repeating-linear-gradient(90deg, #14285A 0 20px, #FFFFFF 20px 40px) over 400 px: 10 periods = 40 stops
for (var k = 0; k < 10; k++) { var a = k * 10, b = a + 5, c = a + 10; rep.push(["#14285A", a], ["#14285A", b], ["#FFFFFF", b], ["#FFFFFF", c]); }
```
Saved (`saved/<key>-3.pptx`, `ppt/slides/slide3.xml`):
```
lin45:          <a:gsLst><a:gs pos="0">FF0000</a:gs><a:gs pos="40000">FFFF00</a:gs><a:gs pos="100000">0000FF</a:gs></a:gsLst><a:lin ang="2700000" scaled="1"/>
lin45unscaled:  ... <a:lin ang="2700000" scaled="0"/>
lin8:           8 <a:gs> at pos 0,14000,...,100000, <a:lin ang="5400000" scaled="1"/>
fillTransparent:<a:solidFill><a:srgbClr val="FF0000"><a:alpha val="50196"/></a:srgbClr></a:solidFill>
rgbaPublic:     <a:solidFill><a:srgbClr val="FF0000"/></a:solidFill>          (Api.RGBA's alpha is dropped)
radial3030:     <a:path path="circle"></a:path>                               (no fillToRect)
background:     <p:bg><p:bgPr><a:gradFill>...<a:lin ang="5400000" scaled="1"/></a:gradFill></p:bgPr></p:bg>
text box:       <p:spPr>...<a:gradFill>...</a:gradFill>...</p:spPr><p:txBody>...
```
Pixel comparison, person's render vs Chromium's CSS (`cmp.py`, mean |diff| rgb):
```
linear-gradient(135deg, red, yellow 40%, blue)  vs ang 45° scaled=1      [16.5, 23.5, 16.5]   wrong
                                               vs ang 45° scaled=0      [0.3, 0.4, 0.3]      identical
linear-gradient(90deg / 180deg (8 stops) / 0 / 60 / 200 / 300deg)  scaled=0   0.1–0.4             identical
linear-gradient(to bottom right, ...)           vs ang 45° scaled=1      [0.2, 0.4, 0.3]      identical
                                               vs ang 45° scaled=0      [13.7, 24.6, 13.8]   wrong
repeating-linear-gradient(90deg, ... 0 20px, ... 20px 40px) as 40 stops  [9.8, 9.2, 7.0]   (stripe edges only: resampling)
slide background linear-gradient(160deg), scaled=0                        [0.3, 0.3, 0.3]
rgba(255,0,0,.5) vs transparent = 128                                    [0.2, 0.2, 0.2]
radial-gradient(#FFF, #000080) vs path=circle                            [27.9, 27.9, 13.9]   (circle, not ellipse)
```
LibreOffice's render of the saved file (`slide2/lo-fills.png`): linear fills and alpha as in the editor; both radial
boxes run **from the top-left corner** (the spec's meaning of a path gradient with no `fillToRect`).

Gotchas:
- Mapping: CSS angle `a` deg -> `((a - 90) mod 360) * 60000`, with `scale = false`. CSS corner keywords
  (`to bottom right` …) are the one case for `scale = true` (the 45°-family angle: to bottom right 45°, to bottom left
  135°, to top left 225°, to top right 315°); `to right/bottom/left/top` = 90/180/270/0deg CSS. Stop positions:
  CSS % x 1000; a stop without a position gets CSS's even spacing (runtime-office fills them in from the computed style).
- `Api.RGBA(r,g,b,a)` + `CreateSolidFill` keeps `RGBA.A` in memory only: the render and the file are opaque. Use
  `fill.UniFill.transparent = round(alpha * 255)` (saved as `<a:alpha val="50196"/>` for 128). rgba gradient stops
  (a scrim: `linear-gradient(180deg, rgba(0,0,0,0), rgba(0,0,0,.7))` over a picture, `slide2/f3.js`): an alpha
  colour modifier on the stop's colour, `st.Gs.color.addColorMod(new AscFormat.CColorMod("alpha", round(a * 100000)))`,
  saved `<a:gs pos="0"><a:srgbClr val="000000"><a:alpha val="0"/></a:srgbClr></a:gs>…<a:alpha val="70000"/>`,
  person vs Chromium `[1.7, 1.0, 1.2]` (exact).
- Radial: `GradPath.rect` (fillToRect) is never read from pptx (`ReadGradPath` reads only `path`), dropped by
  `createDuplicate`, and not written (setting `path.rect = CSrcRect{30,30,70,70}` saved nothing). `Api.FromJSON`
  also drops it (its reader tests `oGradFill.path.fillToRect`, always undefined). So no centre, no ellipse.
- A shape's whole `opacity` has no single OOXML property: multiply the alpha into the fill (`transparent`), the line
  (`ln.Fill.transparent`) and each run's text fill (section 7). Exact when nothing inside the element overlaps;
  overlapping parts (a stroke over its own fill) composite differently from CSS's group opacity. A container's opacity
  over several children = picture (or multiply into each child, approximate).

#### 2. Shapes

**Answer:** all native and pixel-identical to Chromium. Rounded rectangle = preset `roundRect` with
`adj = min(r / min(w, h), 0.5) * 100000` (pill = 50000); four different radii = a public **custom geometry** (one
path of lines and arcs, exact); `ellipse`, `diamond`; arrows = `rightArrow`/`leftArrow`/`upArrow`/`downArrow` with
`adj1` = shaft thickness / cross size x 100000 and `adj2 = 0.4 * length / min(w, h) * 100000` (head = last 40%).
Lines: `<hr>` and `<x-shape kind="line">` as a `p:sp` with the `line` preset geometry (not `Api.CreateShape("line")`,
which makes a connector, see gotchas).

Calls (`slide2/s1.js`, verbatim):
```js
function rounded(x, y, w, h, r) {                      // CSS border-radius r px -> roundRect adj (1/100000 of min side)
  var sh = shape("roundRect", x, y, w, h, blue());
  var g = sh.GetGeometry(); g.SetAdjValue("adj", Math.round(Math.min(r / Math.min(w, h), 0.5) * 100000));
  sh.SetGeometry(g);
  return sh;
}
function corners(x, y, w, h, r) {                      // r = [TL, TR, BR, BL] px; path units = EMU
  var g = Api.CreateCustomGeometry(), p = g.AddPath(), W = w * E, H = h * E, R = r.map(function (v) { return v * E; });
  p.SetWidth(W); p.SetHeight(H);
  p.MoveTo(R[0], 0);
  p.LineTo(W - R[1], 0); p.ArcTo(R[1], R[1], 270 * 60000, 90 * 60000);
  p.LineTo(W, H - R[2]); p.ArcTo(R[2], R[2], 0, 90 * 60000);
  p.LineTo(R[3], H);     p.ArcTo(R[3], R[3], 90 * 60000, 90 * 60000);
  p.LineTo(0, R[0]);     p.ArcTo(R[0], R[0], 180 * 60000, 90 * 60000);
  p.Close();
  var sh = shape("rect", x, y, w, h, blue());
  sh.SetGeometry(g);
  return sh;
}
function arrow(preset, x, y, w, h, shaftFrac) {
  var sh = shape(preset, x, y, w, h, blue()), len = (preset === "upArrow" || preset === "downArrow") ? h : w;
  var g = sh.GetGeometry();
  g.SetAdjValue("adj1", Math.round(shaftFrac * 100000));
  g.SetAdjValue("adj2", Math.round(0.4 * len / Math.min(w, h) * 100000));
  sh.SetGeometry(g);
  return sh;
}
shape("ellipse", 1360, 40, 400, 200, blue()); shape("diamond", 40, 300, 400, 200, blue());
// line: a rect given the line preset, then height 0 set explicitly (slide2/st1.js)
var l = shape("rect", x, y, w, 0, Api.CreateNoFill(), Api.CreateStroke(px * E, solid(hex)));
l.SetGeometry(Api.CreatePresetGeometry("line")); l.SetSize(w * E, 0);
```
Returned: `["round40","roundRect",[20000,..]] ["pill","roundRect",[50000,..]] ["corners",null,"custom"] ["rightArrow","rightArrow",[null,50000,80000]] ["upArrow","upArrow",[null,50000,75000]]`.
Saved (`saved/<key>-13.pptx`, slide4.xml):
```
round40:  <a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 20000"/></a:avLst></a:prstGeom>
corners:  <a:custGeom>…<a:path w="2540000" h="1270000" fill="norm" stroke="1" extrusionOk="0"><a:moveTo><a:pt x="63500" y="0"/></a:moveTo>
          <a:lnTo><a:pt x="2286000" y="0"/></a:lnTo><a:arcTo wR="254000" hR="254000" stAng="16200000" swAng="5400000"/> … <a:close/></a:path></a:custGeom>
rightArrow: <a:prstGeom prst="rightArrow"><a:avLst><a:gd name="adj1" fmla="val 50000"/><a:gd name="adj2" fmla="val 80000"/></a:avLst></a:prstGeom>
hr (st1): <p:sp>… <a:ext cx="5079999" cy="0"/> <a:prstGeom prst="line"/> <a:ln w="25400">…C81E1E…</a:ln>
```
Pixel comparison vs Chromium (`ref-shapes.html`: `border-radius:40px`, `100px`, `10px 40px 80px 20px`, `50%`,
diamond and arrow as `clip-path` polygons with the head from 60%, shaft 25%–75%):
```
round40 [0.2,0.2,0.1]  pill [0.7,0.6,0.4]  corners [0.2,0.2,0.1]  ellipse [1.2,1.0,0.6]  diamond [1.2,1.0,0.6]  rightArrow [1.7,1.4,0.9]
```
The person's render: `slide2/shot-shapes-person.png`.

Gotchas:
- **`Api.CreateShape("line")` (and the connector presets) makes a `CConnectionShape`** (`p:cxnSp`): it throws if the
  current slide already holds drawings added in this command (section 0), and **`ApiSlide.GetAllDrawings()` does not
  return connection shapes** (they were in `Slide.cSld.spTree` but missing from `GetAllDrawings`, `GetAllShapes`).
  A reader that walks `GetAllDrawings` misses every connector a person draws: runtime-office's slide reader must walk
  `slide.Slide.cSld.spTree` (internal) or at least add `CConnectionShape`s from it.
- **A height (or width) of 0 in `Api.CreateShape` becomes 914400 EMU (1 inch)**: the first `<hr>` came out as a
  diagonal (`<a:ext cx="5079999" cy="914400"/>`). `SetSize(w, 0)` afterwards gives `cy="0"`.
- `ApiGeometry.SetAdjValue` is history-tracked (`CChangesGeometryAddAdj`); set it, then `SetGeometry(g)`.
- CSS clamps radii whose sum exceeds a side (all scaled by the same factor): runtime-office takes the computed
  radii from Chromium (`getComputedStyle` gives the specified values; clamp them itself: f = min(1, w/(tl+tr), …)).
- Percent radii (`50%` on a non-square box = an ellipse, elliptical corners): `border-radius: 50%` on a rectangle =
  `ellipse`; other elliptical radii (`a / b` or `%` on a non-square box) need `ArcTo(wR, hR, …)` with wR ≠ hR —
  same custom geometry, not tested beyond the circle case.
- The arrow's shaft thickness is not in format.md; 50% matched a 25%–75% shaft polygon exactly. runtime-office should
  take it from Claude Slides' renderer (unknown here).

#### 3. Strokes

**Answer:** width, solid, dashed, dotted and rgba strokes are native (public `Api.CreateStroke(emu, fill, dash)`,
internal `ln.Fill.transparent` for alpha and `ln.setCap(1)` for round dots); a CSS border is inside the box, a shape's
line is centred on its edge, so **inset the geometry by half the width** (exact; `algn="in"` is saved but the editor
ignores it). **Double is not rendered by the editor** (`cmpd="dbl"` is saved, drawn as one thick line): draw it as
one custom-geometry shape with a fill-only path and two stroke-only rectangles (tested, exact). **Borders on some sides = one filled rectangle per side** (solid, exact) or a `line` shape per side
(dashed/dotted).

Calls (`slide2/st1.js`, verbatim):
```js
function stroke(px, hex, dash) { return Api.CreateStroke(px * E, solid(hex), dash); }
put(s, shape("rect", 40 + 4, 40 + 4, 400 - 8, 200 - 8, pale(), stroke(8, "#C81E1E")), "border8inset");
var inS = stroke(8, "#C81E1E"); inS.Ln.setAlgn(1);                                   // internal; editor ignores it
put(s, shape("rect", 920 + 3, 40 + 3, 400 - 6, 200 - 6, pale(), stroke(6, "#C81E1E", "dash")), "dashed6");
var dot = stroke(6, "#C81E1E", "sysDot"); dot.Ln.setCap(1);                          // internal: round caps = round dots
var dbl = stroke(12, "#C81E1E"); dbl.Ln.setCmpd(0);                                  // internal: cmpd dbl
var a = stroke(16, "#C81E1E"); a.Ln.Fill.transparent = 128;                          // internal: stroke alpha
put(s, shape("rect", 920, 300, 400, 200, pale()), "sidesBox");                       // border-top 8 + border-left 4:
put(s, shape("rect", 920, 300, 400, 8, solid("#C81E1E")), "sidesTop");
put(s, shape("rect", 920, 300, 4, 200, solid("#C81E1E")), "sidesLeft");
var dl = sideLine(1360, 300 + 3, 400, 0, stroke(6, "#C81E1E", "dash")); dl.SetSize(400 * E, 0);   // dashed top only
```
Saved (`saved/<key>-14.pptx`):
```
border8inset: <a:off x="279399" y="279399"/><a:ext cx="2489199" cy="1219199"/>  <a:ln w="50800">…<a:prstDash val="solid"/></a:ln>
border8algnIn:<a:ln w="50800" algn="in">
dashed6:      <a:ln w="38100">…<a:prstDash val="dash"/></a:ln>
dotted6:      <a:ln w="38100" cap="rnd">…<a:prstDash val="sysDot"/></a:ln>
double12:     <a:ln w="76200" cmpd="dbl">
rgba16:       <a:ln w="101600"><a:solidFill><a:srgbClr val="C81E1E"><a:alpha val="50196"/></a:srgbClr></a:solidFill>
dashTop:      <a:ext cx="2539999" cy="0"/> <a:ln w="38100">…dash…
```
Pixel comparison vs Chromium `border:` (`slide2/cmp-strokes.png`: editor rows on top, Chromium below):
```
8px solid, geometry inset   [0.4, 4.1, 4.2]   = edge anti-aliasing only
8px solid, algn="in"        [1.1, 10.6, 11.2] drawn centred: the box is 8 px larger
6px dashed                  [0.5, 4.9, 5.2]   editor dashes are longer (prstDash "dash" = 4w on, 3w off)
6px dotted (sysDot + rnd)   [0.5, 4.4, 4.6]
12px double (cmpd dbl)      [1.6, 15.4, 16.3] drawn as one thick line
16px rgba, inset            [1.3, 2.3, 2.0]
top 8 + left 4 as rects     [0.1, 1.2, 1.2]
dashed top as a line shape  [0.1, 1.3, 1.4]
```
LibreOffice (`slide2/lo-strokes.png`) honours `algn="in"` and draws `cmpd="dbl"` as a thick line with a hairline gap.

Gotchas:
- CLn setters (`setAlgn`, `setCap`, `setCmpd`, `setHeadEnd`, `Fill.transparent`) are plain assignments: set them on
  the `ApiStroke.Ln` **before** `CreateShape`/`SetLine` (the history records the whole `ln` at `spPr.setLn`).
- Width unit: EMU (`a:ln w`), so 1 px = 6350. CSS caps the format's border at 16 px (32 for shapes/connectors).
- Double, tested (`slide2/st2.js`): CSS draws two lines of w/3 with a w/3 gap; one shape does it, line width w/3:
  ```js
  function rectPath(inset, fill, stroke) {
    var p = g.AddPath(), i = inset * E; p.SetWidth(W); p.SetHeight(H); p.SetFill(fill); p.SetStroke(stroke);
    p.MoveTo(i, i); p.LineTo(W - i, i); p.LineTo(W - i, H - i); p.LineTo(i, H - i); p.Close();
  }
  rectPath(0, "norm", false); rectPath(bw / 6, "none", true); rectPath(bw * 5 / 6, "none", true);
  var sh = shape("rect", x, y, w, h, solid(fillHex), Api.CreateStroke(bw / 3 * E, solid(lineHex))); sh.SetGeometry(g);
  ```
  vs Chromium `border:12px double`: `[0.5, 5.1, 5.5]` (edges only); saved as three `<a:path>`s
  (`fill="norm" stroke="0"`, then two `fill="none" stroke="1"`).
- rgba border over a fill: CSS paints the background under the border (`background-clip: border-box`); with the
  inset geometry the fill stops at the stroke's centre line, so a translucent stroke is two-toned. Exact form: a
  fill-only shape at the full box + a stroke-only shape inset (two shapes).
- Rounded box with sides of different widths/styles: no native form; picture (rare).

#### 4. Connectors

**Answer:** native. A straight connector = a `line`-preset `p:sp` over the bounding box with `SetFlipH`/`SetFlipV`
for direction; `hv`, `vh` and `elbow` = an open **custom-geometry polyline** (exact corners; the preset
`bentConnector3` also works for elbow). Heads = internal `CLn.headEnd`/`tailEnd` (`AscFormat.EndArrow`, type `Arrow`
= open, `Triangle` = filled; head = path start = (x1, y1), tail = path end); the editor draws heads on custom paths
too. Dashed/dotted and 2–32 px as in section 3. All saved as plain `p:sp` (not `p:cxnSp`, deliberately: section 2).

Calls (`slide2/c1.js`, verbatim):
```js
function end(kind) {                                  // none | open | filled
  if (!kind || kind === "none") return null;
  var a = new AscFormat.EndArrow();
  a.type = kind === "open" ? AscFormat.LineEndType.Arrow : AscFormat.LineEndType.Triangle;
  a.w = AscFormat.LineEndSize.Mid; a.len = AscFormat.LineEndSize.Mid;
  return a;
}
function lineStroke(px, hex, dash, head, tail) {
  var st = Api.CreateStroke(px * E, solid(hex), dash);
  if (dash === "sysDot") st.Ln.setCap(1);
  st.Ln.setHeadEnd(end(head)); st.Ln.setTailEnd(end(tail));     // head = path start (x1,y1), tail = path end (x2,y2)
  return st;
}
function straight(x1, y1, x2, y2, st) {               // a line-preset p:sp over the bounding box, flipped
  var x = Math.min(x1, x2), y = Math.min(y1, y2), w = Math.abs(x2 - x1), h = Math.abs(y2 - y1);
  var l = shape("rect", x, y, w || 1, h || 1, Api.CreateNoFill(), st);
  l.SetGeometry(Api.CreatePresetGeometry("line"));
  l.SetSize(w * E, h * E);
  if (x2 < x1) l.SetFlipH(true);
  if (y2 < y1) l.SetFlipV(true);
  return l;
}
function polyline(points, st) {                       // open custom-geometry path through canvas points
  var xs = points.map(function (p) { return p[0]; }), ys = points.map(function (p) { return p[1]; });
  var x = Math.min.apply(null, xs), y = Math.min.apply(null, ys), w = Math.max.apply(null, xs) - x, h = Math.max.apply(null, ys) - y;
  var g = Api.CreateCustomGeometry(), p = g.AddPath();
  p.SetWidth(w * E); p.SetHeight(h * E); p.SetFill("none");
  points.forEach(function (pt, i) { var X = (pt[0] - x) * E, Y = (pt[1] - y) * E; if (i) p.LineTo(X, Y); else p.MoveTo(X, Y); });
  var sh = shape("rect", x, y, w, h, Api.CreateNoFill(), st);
  sh.SetGeometry(g);
  return sh;
}
function route(kind, x1, y1, x2, y2) {
  if (kind === "hv") return [[x1, y1], [x2, y1], [x2, y2]];
  if (kind === "vh") return [[x1, y1], [x1, y2], [x2, y2]];
  var xm = (x1 + x2) / 2; return [[x1, y1], [xm, y1], [xm, y2], [x2, y2]];   // elbow
}
straight(460, 480, 60, 320, lineStroke(6, "#C81E1E", "dash", "open", "open"));
polyline(route("elbow", 1860, 560, 1560, 360), lineStroke(4, "#C81E1E", null, "none", "filled"));
```
Saved (`saved/<key>-15.pptx`):
```
straightEndFilled:      <a:xfrm rot="0" flipH="0" flipV="0">… <a:prstGeom prst="line"/> <a:ln w="25400">…<a:tailEnd type="triangle" w="med" len="med"/></a:ln>
straightBothOpenDashed: <a:xfrm rot="0" flipH="1" flipV="1">… <a:prstDash val="dash"/><a:headEnd type="arrow" w="med" len="med"/><a:tailEnd type="arrow" …/>
verticalStartFilledDotted: <a:ext cx="0" cy="2539999"/> <a:ln w="12700" cap="rnd">…sysDot…<a:headEnd type="triangle" …/>
wide32BothFilled:       <a:ln w="203200">…<a:headEnd type="triangle" w="med" len="med"/><a:tailEnd type="triangle" w="med" len="med"/>
hv:     <a:custGeom><a:pathLst><a:path w="1905000" h="1270000" fill="none" stroke="1" extrusionOk="0"><a:moveTo><a:pt x="0" y="0"/></a:moveTo>
        <a:lnTo><a:pt x="1905000" y="0"/></a:lnTo><a:lnTo><a:pt x="1905000" y="1270000"/></a:lnTo></a:path></a:pathLst></a:custGeom>
elbowBackwards: <a:moveTo><a:pt x="1905000" y="1270000"/></a:moveTo><a:lnTo><a:pt x="952500" y="1270000"/></a:lnTo>…<a:lnTo><a:pt x="0" y="0"/></a:lnTo>
bentConnector3: <a:prstGeom prst="bentConnector3"><a:avLst><a:gd name="adj1" fmla="val 50000"/></a:avLst></a:prstGeom>
```
The person's render (`slide2/shot-connectors-person.png`): every line, flip, bend, dash and head where asked; the
dotted vertical has its filled head at the top (its start); the elbow drawn backwards has its head at (1560, 360).
LibreOffice (`slide2/lo-connectors.png`) draws the same geometry and heads.

Gotchas:
- Head size is relative to the line width (`sm`/`med`/`lg` = 2/3/5 x width): a 32 px connector with `med` heads has
  96 px heads. Pick the size that matches Claude Slides' head (not in format.md; measure its render).
- `ApiDrawing.SetFlipH/SetFlipV` are public; a line with w or h = 0 must get `SetSize` after `SetGeometry` (section 2).
- `AscFormat.LineEndType` / `LineEndSize` are reachable in commands (unlike `Asc.*`).
- A real `p:cxnSp` (glued to shapes, `stCxn`/`endCxn`) is not needed by the format and is invisible to
  `GetAllDrawings`; plain `p:sp` lines are what a person can still select, move and restyle.

#### 5. Effects (shadows)

**Answer:** an outer box-shadow is native through an internal `spPr.setEffectPr` (`COuterShdw`: offset as dist/dir,
blur, spread as sx/sy, rgba colour) and is saved exactly, **but the editor draws every shadow unblurred** (a hard
copy of the geometry; `drawShdw` has no blur). So: `blur = 0` shadows native (exact in the editor, LibreOffice and
the file); **blurred shadows = Chromium's own shadow picture behind the native shape, grouped with it** (exact,
0.3 diff, moves with the shape). **Inset shadows: picture** (saved as `innerShdw`, drawn by neither the editor nor
LibreOffice). **Text shadow: no native run property** (`CTextPr` has no shadow/effect field); a no-fill text box's
`outerShdw` is drawn on the text by LibreOffice/PowerPoint but **not** by the editor; so text-shadow = a shadow-only
Chromium picture (`color: transparent; text-shadow: …`) behind the native text box, grouped (same as box-shadow), or
for `blur 0` a second text box in the shadow colour offset behind it.

Calls (`slide2/e1.js`, verbatim):
```js
function shadowColor(hex, alpha) {
  var c = rgb(hex), u = AscFormat.CreateUniColorRGB(c[0], c[1], c[2]);
  if (alpha < 1) u.addColorMod(new AscFormat.CColorMod("alpha", Math.round(alpha * 100000)));
  return u;
}
function effects(sh) {                                 // sh = CSS box-shadow {x, y, blur, spread, color, alpha, inset, w, h}
  var lst = new AscFormat.CEffectLst(), x = sh.x, y = sh.y, e;
  e = sh.inset ? new AscFormat.CInnerShdw() : new AscFormat.COuterShdw();
  e.color = shadowColor(sh.color, sh.alpha);
  e.blurRad = Math.round(sh.blur * E);
  e.dist = Math.round(Math.sqrt(x * x + y * y) * E);
  e.dir = Math.round(((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360 * 60000);
  if (!sh.inset) {
    e.algn = AscFormat.RECT_ALIGN_CTR; e.rotWithShape = false;
    if (sh.spread) { e.sx = Math.round((sh.w + 2 * sh.spread) / sh.w * 100000); e.sy = Math.round((sh.h + 2 * sh.spread) / sh.h * 100000); }
    lst.outerShdw = e;
  } else lst.innerShdw = e;
  var pr = new AscFormat.CEffectProperties(); pr.EffectLst = lst;
  return pr;
}
function withShadow(d, sh) { d.Drawing.spPr.setEffectPr(effects(sh)); return d; }   // history-tracked setter
```
The grouped shadow picture (`slide2/e2.js` + `slide2/shadow-png.mjs`, which screenshots a transparent box carrying
the same `box-shadow` with `omitBackground: true`: Chromium paints an outer shadow only outside the border box):
```js
var pad = 60, x = 60, y = 60, w = 400, h = 200;
var pic = Api.CreateImage(scope.png, (w + 2 * pad) * E, (h + 2 * pad) * E); pic.SetPosition((x - pad) * E, (y - pad) * E);
var box = shape("rect", x, y, w, h, solid("#DDE6F5"));
var grp = Api.CreateGroup([pic, box]);
slide.AddObject(grp); grp.SetName("shadowGroup");     // name AFTER AddObject (see gotchas)
-> {"kind":"group","n":1}; person vs Chromium box-shadow:0 8px 24px rgba(0,0,0,.25): [0.3, 0.2, 0.3]
```
Saved (`saved/<key>-16.pptx`, `-17.pptx`):
```
soft:        <a:outerShdw blurRad="152400" dist="50800" dir="5400000" algn="ctr" rotWithShape="0"><a:srgbClr val="000000"><a:alpha val="25000"/></a:srgbClr></a:outerShdw>
roundSpread: <a:outerShdw blurRad="0" dist="107763" dir="2700000" sx="104000" sy="108000" algn="ctr" rotWithShape="0">…C81E1E…<a:alpha val="50000"/>
inset:       <a:innerShdw blurRad="127000" dist="0" dir="0">…<a:alpha val="50000"/></a:innerShdw>
picture:     <p:pic>…<a:outerShdw blurRad="0" dist="143684" dir="2700000" …>
group:       <p:grpSp>…<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="3301999" cy="2031999"/><a:chOff …/><a:chExt …/></a:xfrm></p:grpSpPr>
             <p:pic>…<a:ext cx="3301999" cy="2031999"/>…</p:pic><p:sp>…<a:off x="380999" y="380999"/>…DDE6F5…</p:sp></p:grpSp>
```
Person vs Chromium (`ref-effects.html`; `slide2/shot-effects-person.png`, `slide2/lo-effects.png`):
```
0 8px 24px rgba(0,0,0,.25)       [3.1, 3.1, 3.2]   editor: a hard grey band under the box; LibreOffice: blurred, right
12px 12px 0 #14285A              [2.0, 1.9, 1.4]   exact (edges)
12px 12px 0 8px rgba(..,.5), r40 [0.3, 1.1, 1.1]   exact: spread via sx/sy, alpha, follows the roundRect
inset 0 0 20px rgba(0,0,0,.5)    [4.3, 4.4, 4.7]   not drawn (editor nor LibreOffice)
picture 16px 16px 0 rgba(..,.4)  [3.3, 1.8, 2.2]   drawn (hard), right offset
text-shadow 6px 6px 0 (shape shadow on a no-fill text box)  [15.6, 24.3, 23.0]  editor draws nothing; LibreOffice draws it on the glyphs
```
Gotchas:
- Units: `dist`, `blurRad` EMU; `dir` 1/60000 deg clockwise from +x (CSS y grows down, so `atan2(y, x)` is right);
  `sx/sy` 1/1000 % with `algn="ctr"` to grow the shadow evenly (= spread). Several CSS shadows: OOXML has one
  `outerShdw` per effect list: keep the first (or the largest) natively, or draw them all as the grouped picture.
- CSS `blur` B is a Gaussian of sigma B/2; OOXML `blurRad` is the blur radius: `blurRad = B` looked right in
  LibreOffice; PowerPoint not checked.
- **Group names: call `SetName` after `AddObject`** (before, the group saved `name=""` and read back
  "Group 216441597"). **Names are unique per deck:** `ApiDrawing.SetName` blanks any other drawing already holding
  that name (`b.SetName(a.GetName())` turned `a` into "Round corner rectangle 523925280"); runtime-office must keep
  its names unique (e.g. prefix with the slide id).
- A grouped shadow picture is a separate picture: if the person resizes the group, the shadow scales with it (fine);
  if they ungroup and move the shape, the shadow stays (acceptable; it is a picture).

#### 6. Transforms

**Answer:** rotation is native on shapes, text boxes and pictures (public `ApiDrawing.SetRotation(degrees)`, about
the centre like CSS's default origin); flips are public (`SetFlipH`/`SetFlipV`, used for connectors); **skew is
native only for a painted box without text** (a parallelogram custom geometry, exact); skewed text or a skewed
picture = picture. `translate` is just position; `scale(N)` = position and size (and, for text, font size) x N,
taken from Chromium's measured box.

Calls (`slide2/t1.js`, verbatim):
```js
var r = shape("rect", 100, 100, 400, 200, solid("#2F5597")); r.SetRotation(30);
var tb = textBox(700, 150, 500, 100, "Rotated text", 96, "#14285A", solid("#DDE6F5")); tb.SetRotation(-15);
var im = Api.CreateImage(scope.png, 400 * E, 300 * E); im.SetPosition(1400 * E, 100 * E); im.SetRotation(20);
// skewX(-20deg) of a 400x200 box about its centre: top edge shifts right by h/2*tan(20), bottom left
var w = 400, h = 200, dx = h / 2 * Math.tan(20 * Math.PI / 180);
var g = Api.CreateCustomGeometry(), p = g.AddPath(), W = (w + 2 * dx) * E, H = h * E, D = 2 * dx * E;
p.SetWidth(W); p.SetHeight(H);
p.MoveTo(D, 0); p.LineTo(W, 0); p.LineTo(W - D, H); p.LineTo(0, H); p.Close();
var sk = shape("rect", 100 - dx, 600, w + 2 * dx, h, solid("#C81E1E")); sk.SetGeometry(g);
var fl = shape("rightArrow", 700, 600, 400, 200, solid("#008000")); fl.SetFlipH(true);
-> {"rot":[29.999999999999996,-14.999999999999998,20]}
```
Saved (`saved/<key>-18.pptx`): `rot30 <a:xfrm rot="1799999" …>`, `textRot-15 <a:xfrm rot="-899999" …>`,
`picRot20 <a:xfrm rot="1200000">`, `flipH <a:xfrm rot="0" flipH="1" flipV="0">`, skew = `custGeom` with `rot="0"`.
Person vs Chromium (`ref-transforms.html`, `slide2/shot-transforms-person.png`):
```
rotate(30deg) shape        [0.6, 0.5, 0.3]
rotate(20deg) picture      [1.4, 0.8, 0.9]
skewX(-20deg) box          [0.3, 1.2, 1.2]
rotate(-15deg) text box    [7.5, 7.0, 5.6]   rotation right; the text wrapped (CSS had nowrap: section 7)
```
Gotchas:
- **Negative angles are saved negative** (`rot="-899999"`): normalise to 0–360 before `SetRotation`
  (`(deg % 360 + 360) % 360`), as PowerPoint writes them.
- runtime-office must measure the **untransformed** box (offsetLeft/Top/Width/Height chain, not
  `getBoundingClientRect`, which returns the rotated bounds) and pass the angle separately; CSS
  `transform-origin` is always the centre in this format, as in OOXML.
- Tables cannot rotate: `slide2/t4.js` `Api.CreateTable(2, 2).SetRotation(10)` -> `{"setRotation":false,"rotation":0,"canRotate":false}`.
  A rotated `<table>` = picture.
- Rotated + skewed together, or skew on text/picture/x-icon: picture.

#### 7. Text

**Answer:** native: letter spacing (`ApiRun.SetSpacing(twips)`, 1 px = 10 twips; line widths equal Chromium's to
0.01 px), uppercase (`SetCaps(true)` → `cap="all"`, or upper-case the text), strikethrough (`SetStrikeout`), single
underline, outlined text (`SetOutLine(stroke)` → `a:ln` in `a:rPr`), **gradient text through `ApiRun.SetFill(gradient)`**
(not `SetTextFill`, which the pptx file drops), text opacity (`SetFill` of a solid fill with internal
`transparent`), `nowrap` (internal `bodyPr.wrap = nTWTNone` via the history-tracked `txBody.setBodyPr`).
lowercase/capitalize: transform the text itself (OOXML has only `cap` none/small/all). **Not native:** double,
dotted, dashed, wavy underline and an underline colour (`CTextPr.Underline` is a boolean; no `uFill`): nearest native
is a single underline in the text colour, else picture. (Text shadow: section 5.)

Calls (`slide2/t2.js`, `t3.js`, verbatim; run at 64 px Arial):
```js
box("spacing10px", 130, "AVATAR spacing", function (r) { r.SetSpacing(100); });        // 10 px = 5 pt = 100 twips
box("caps", 220, "Uppercase via caps", function (r) { r.SetCaps(true); });
box("strike", 310, "Struck through", function (r) { r.SetStrikeout(true); });
box("underline", 400, "Underlined", function (r) { r.SetUnderline(true); });
box("outline", 490, "Outlined text", function (r) { r.SetOutLine(Api.CreateStroke(2 * E, solid("#C81E1E"))); });
box("opacity50", 670, "Half opaque text", function (r) { var f = solid("#14285A"); f.UniFill.transparent = 128; r.SetFill(f); });
// nowrap: inside box(), after the content
var bp = sh.Shape.getBodyPr().createDuplicate(); bp.wrap = AscFormat.nTWTNone; sh.Shape.txBody.setBodyPr(bp);
// gradient text (t3.js)
var f = Api.CreateLinearGradientFill([Api.CreateGradientStop(col("#C81E1E"), 0), Api.CreateGradientStop(col("#2F5597"), 100000)], 0);
f.UniFill.fill.lin.scale = false; r.SetFill(f);
```
Line widths (`slide2/t2m.js`: `Paragraph.Lines[0].Ranges[0].W` in the next command; Chromium `slide2/chrome-widths.mjs`
with `font-kerning:none; font-variant-ligatures:none`):
```
editor:   plain 494.43  spacing10px 634.43  caps 707.78  strike 416.22  underline 309.53  outline 359.31  opacity50 466.09  nowrap 1533.24 (1 line)  wrapped 5 lines
Chromium: plain 494.44  spacing10px 634.44  caps 707.78  strike 416.22  underline 309.53  outline 359.31  opacity50 466.09  nowrap 1533.25
```
Saved (`saved/<key>-19.pptx`, `-20.pptx`), `a:rPr` of each:
```
spacing10px: <a:rPr sz="3200" spc="499">          caps: cap="all"     strike: strike="sngStrike"     underline: u="sng"
outline:     <a:rPr sz="3200"><a:ln w="12700"><a:solidFill><a:srgbClr val="C81E1E"/></a:solidFill>…</a:ln><a:solidFill>14285A…
opacity50:   <a:solidFill><a:srgbClr val="14285A"><a:alpha val="50196"/></a:srgbClr></a:solidFill>
gradient via SetTextFill: <a:solidFill><a:srgbClr val="14285A"/></a:solidFill>   (the gradient is NOT saved; editor showed it)
gradient via SetFill:     <a:gradFill><a:gsLst><a:gs pos="0">C81E1E</a:gs><a:gs pos="100000">2F5597</a:gs></a:gsLst><a:lin ang="0" scaled="0"/></a:gradFill>
nowrap:      <a:bodyPr … wrap="none" …>        wrapped: wrap="square"
```
The person's render (`slide2/shot-text-person.png`, `crop-text2.png`) shows each style; LibreOffice
(`slide2/lo-text.png`) draws all but the outline and the text gradient (LibreOffice limits, the XML is standard).

Gotchas:
- `SetSpacing` takes twips and is stored in mm (`spc="499"` for 500): ±0.01 pt.
- Gradient text: the editor stretches the gradient over the run's text; CSS `background-clip:text` stretches it
  over the element's box. Same when the element is shrink-wrapped (a pinned `<p>` with no width); for a wide box, set
  the stops from Chromium's box/text ratio, or accept the difference.
- `-webkit-text-stroke` in CSS is centred on the glyph outline like `a:ln`; widths match (2 px = 12700 EMU).
- Text opacity via the run fill multiplies with nothing else: for `opacity` on a whole text box, apply it to every
  run's fill and the box's own fill/line (section 1).
- Observation for text fidelity (not this brief's scope, measured once): in a 64 px Arial line at 1.2 spacing the
  editor's first baseline is 62.65 px below the line top (`Lines[0].Y`; it splits the leading in proportion to
  ascent/descent), Chromium's 60 (CSS splits it equally; 60.6 unrounded): text sits ~2 px (0.03 em) lower in the
  editor than in Chromium. Worth a dedicated check before matching vertical positions to the pixel.

#### 8. Pictures

**Answer:** all native and pixel-identical. `contain` = size the picture to the fitted box (runtime-office's math);
`cover` and `data-crop` = a crop, internal `blipFill.srcRect` set through `CImageShape.setBlipFill` (saved as a
standard `<a:srcRect>`); rounded corners = the picture's own geometry `roundRect` (internal `spPr.setGeometry`);
opacity = internal `CImageShape.setTransparent(0..255)` (saved `<a:alphaModFix>`). **Set crop and opacity before
`AddObject` on a new picture; never change them on an existing picture inside a command** (not sent to the person,
see gotchas): replace the picture instead.

Calls (`slide2/p1.js`, verbatim; source PNG 400x300):
```js
function pic(x, y, w, h) { var im = Api.CreateImage(scope.png, w * E, h * E); im.SetPosition(x * E, y * E); return im; }
function crop(im, l, t, r, b) {                       // insets in percent
  var bf = im.Drawing.blipFill.createDuplicate();
  bf.srcRect = new AscFormat.CSrcRect(l, t, 100 - r, 100 - b);     // sdkjs keeps r/b as edge positions
  im.Drawing.setBlipFill(bf);
  return im;
}
put(s, crop(pic(60, 60, 300, 300), 12.5, 0, 12.5, 0), "cover300");            // cover 400x300 into 300x300
put(s, pic(460, 60 + 37.5, 300, 225), "contain300");                           // contain = 300x225, centred
var rr = pic(860, 60, 400, 300), g = AscFormat.CreateGeometry("roundRect");
g.setAdjValue("adj", Math.round(40 / 300 * 100000));                           // border-radius 40px
rr.Drawing.spPr.setGeometry(g);
var op = pic(1360, 60, 400, 300); op.Drawing.setTransparent(128);              // opacity .5
var z = 1.5, fx = 0.3, fy = 0.7, vis = 100 / z;                                 // data-crop "1.5 30% 70%"
crop(pic(60, 460, 400, 300), (100 - vis) * fx, (100 - vis) * fy, (100 - vis) * (1 - fx), (100 - vis) * (1 - fy));
```
Saved (`saved/<key>-21.pptx`):
```
cover300:  <p:blipFill rotWithShape="1"><a:blip r:embed="…"/><a:srcRect l="12500" t="0" r="12500" b="0"/><a:stretch/></p:blipFill>
rounded40: <a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 13333"/></a:avLst></a:prstGeom>   (on the p:pic)
opacity50: <a:blip r:embed="…"><a:alphaModFix amt="50196"/></a:blip>
zoom15:    <a:srcRect l="9999" t="23333" r="23333" b="10000"/>
```
Person vs Chromium (`ref-pictures.html`: `object-fit:cover`, `contain`, `border-radius:40px`, `opacity:.5`, an
overflow-hidden 1.5x zoom): `[2.2,1.2,1.4] [4.4,1.9,2.6] [2.4,1.1,1.5] [1.8,0.9,1.4] [1.1,0.5,0.7]` (resampling of
the test image's diagonal only). The person's page read the same `srcRect`, transparency and geometry right after.

Gotchas:
- **`CImageShape.setBlipFill` records no history while a command runs** (`if (!Asc.editor.evalCommand)`, and
  `editor.evalCommand` is `true` inside connector commands). On a picture made in the same command it does not
  matter (the new picture is sent whole). On an existing picture (`slide2/p2.js`, `p4.js`): the agent's copy changed,
  the person's did not (`9.99999` still after 5 s), until a later recorded change dragged it along. Switching the
  flag off around the call sends it (`p3.js`):
  ```js
  var old = editor.evalCommand; editor.evalCommand = false;
  try { d.Drawing.setBlipFill(bf); } finally { editor.evalCommand = old; }
  ```
  Internal on internal; simpler for runtime-office: rebuild the picture (it rebuilds whole slides anyway).
- `CSrcRect(l, t, r, b)` takes **edge positions** in percent (r = 100 − right inset, b = 100 − bottom inset);
  the file gets standard insets (`r="12500"`). Rounding: 1/1000 % (`l="9999"`).
- A cropped picture is also what a person crops in the editor's UI: they can re-crop it natively.
- Rounded picture corners with four different radii: a custom geometry on the picture (`spPr.setGeometry` of the
  section-2 path), not tried on a picture but the same call.

#### 9. Tables

**Answer:** native with the first probe's table builder (`slide/build3.js`) and nothing new. `<tr style="background">`
= the same `cell.SetShd("clear", r, g, b)` on every cell of the row (OOXML has no row fill); percentage column widths
need nothing beyond measuring them to px in Chromium (grid + per-cell widths, as build3 does); the header row = its
cells' fill and run formatting (bold/colour) like any row. Optional: `t.SetTableLook(true, …)` writes
`firstRow="1"` (PowerPoint's "Header Row" box) without visible effect under "No Style, No Grid".

Run (`slide2/spec-table.py` through `slide/build3.js`: 1200 px table, `width:50%/25%/25%` -> 600/300/300 px, a navy
header row of bold white text, a `#EEF2FA` row, a plain row):
```
-> {"slide":"1_1354","index":11,"ids":["1_1398"]}
saved (saved/<key>-22.pptx, slide12.xml):
<a:tblGrid><a:gridCol w="3809999"/><a:gridCol w="1904999"/><a:gridCol w="1904999"/></a:tblGrid>
<a:tr h="381000"> 3 x <a:tcPr marL="76199" marR="76199" marT="50799" marB="50799"><a:solidFill><a:srgbClr val="14285A"/></a:solidFill></a:tcPr>
<a:tr h="381000"> 3 x <a:tcPr …><a:solidFill><a:srgbClr val="EEF2FA"/></a:solidFill></a:tcPr>
<a:tr h="381000"> 3 x <a:tcPr …></a:tcPr>          (no fill: SetShd("nil"))
```
The person's render (`slide2/crop-table.png`): header and shaded row span all three cells, widths 2:1:1.

Gotchas:
- Adjacent filled cells show a faint hairline seam between them in the editor at some zooms (anti-aliased cell
  edges), not in the file. A row background therefore looks very slightly striped vertically on screen.
- `<td>`/`<th>` padding: one value per table in the format; build3's `SetCellMargin*` in twips (1 px = 10).

#### 10. Slides: hidden, transitions, magic move, sections

**Answer:** `hidden` = public `slide.SetVisible(false)` (`show="0"`). Transitions are public:
`Api.CreateSlideShowTransition()` + `SetEntryEffect(name)` + `SetDuration(ms)` + `slide.SetSlideShowTransition(t)`;
fade = **`"effectFadeSmoothly"`** (`"effectFade"` is fade *through black*), push = `"effectPushLeft"` (or
Up/Right/Down), **magic = `"effectMorphByObject"` (Euro-Office has Morph)**, matched across slides by PowerPoint's
`!!name` marker on both shapes, set with the internal `Drawing.setName` because the public `SetName` refuses
duplicates. **Sections: no.** The only route (internal `CPresentation.addSection`) disconnected the person and made
the document unsaveable (status 3/7), exactly like section 0: never touch sections; leave `data-section` out.

Calls (`slide2/sl1a.js`, verbatim):
```js
hid.SetVisible(false);
function transition(slide, effect, ms) {
  var t = Api.CreateSlideShowTransition();
  var ok = t.SetEntryEffect(effect); t.SetDuration(ms);
  slide.SetSlideShowTransition(t);
  return ok;
}
transition(fade, "effectFadeSmoothly", 700);
transition(push, "effectPushLeft", 700);
var a = newSlide("t:magicA", "#FFFFFF"), boxA = shape("roundRect", 100, 400, 300, 300, solid("#2F5597"));
a.AddObject(boxA); boxA.Drawing.setName("!!box");                 // internal setName: no deck-wide uniqueness pass
var b = newSlide("t:magicB", "#FFFFFF"), boxB = shape("roundRect", 1200, 200, 600, 600, solid("#C81E1E"));
b.AddObject(boxB); boxB.Drawing.setName("!!box");
transition(b, "effectMorphByObject", 1000);
-> {"visible":false,"set":[true,true,true],"effects":["effectFadeSmoothly","effectPushLeft","effectMorphByObject"],"names":["!!box","!!box"]}
person (sl1r.js): ["t:hidden",false,"effectNone",500,..] ["t:fade",true,"effectFadeSmoothly",700,..] ["t:push",true,"effectPushLeft",700,..]
                  ["t:magicA",true,"effectNone",500,"!!box"] ["t:magicB",true,"effectMorphByObject",1000,"!!box"]
```
Saved (`saved/<key>-32.pptx`, the `mc:Choice` branch of each slide):
```
t:hidden  <p:sld … show="0">
t:fade    <p:transition spd="med" p14:dur="700" advClick="1"><p:fade thruBlk="0"/></p:transition>
t:push    <p:transition spd="med" p14:dur="700" advClick="1"><p:push dir="l"/></p:transition>
t:magicB  <p:transition spd="slow" p14:dur="1000" advClick="1"><p159:morph option="byObject"/></p:transition>   (Fallback: <p:fade/>)
names     '!!box' on slide6 and slide7
```
Public `SetName` on the second slide's shape blanked the first (`slide2/sl2.js`):
`a.SetName("!!pub"); b.SetName("!!pub")` -> `["Round corner rectangle 1558044712", "!!pub"]`.

Sections (`slide2/sl1b.js`, a fresh key, after `sl1a.js` had left the person fine with `viewMode:false`):
```js
var sec = new AscCommonSlide.CPrSection(); sec.setName(d[0]); sec.setStartIndex(d[1]); sec.setGuid("{0E4D3C2B-…}");
P.addSection(P.Sections.length, sec);
-> {"sections":[["Intro",2],["Demo",5]]}
person 4 s later: {"viewMode":true}; callback: {"status":1,"actions":[{"type":0,"userid":"user:spike"}]}
(first run, same calls: the forcesave answered status 7, the file had no p14:sectionLst, the key closed with status 3)
```
Gotchas:
- Morph needs the same name on both shapes; runtime-office's own naming scheme must then allow one name on two
  slides, which the public `SetName` forbids (it renames the other). Use `Drawing.setName` for `!!` names only,
  keep every other name unique.
- `SetDuration` is milliseconds; `spd` is derived (500 fast, 750 med, 1000 slow).
- Push direction: Claude Slides' push is not specified in format.md; `effectPushLeft` saves `dir="l"` (the new
  slide travels leftward, entering from the right).
- `transition.SetEntryEffect` returns false for unknown names; the full list (with directions, Morph by word/char)
  is `ApiSlideShowTransition.ENTRY_EFFECT_MAP` in the SDK (`slide2/g.sh 'ENTRY_EFFECT_MAP='`).

#### 11. Builds (animations)

**Answer:** native, and mostly public. `slide.GetTimeLine().GetMainSequence().AddEffect(drawing, type, trigger)`
(types from `ApiAnimationSequence.EFFECT_TYPE_MAP`: `"fade"`, `"zoom"`, `"bounce"`, `"fade-out"`, …; triggers
`"onclick"` / `"withprevious"` / `"afterprevious"`), order = add order (or `ApiAnimationEffect.MoveTo(i)`), "auto" =
`"afterprevious"`. Directions (rise = fly in from the bottom, drop = from the top, left, right) need a preset
**subtype** the public `AddEffect` does not take: the same three internal steps it runs, with the subtype
(`Timing.createEffect(id, class, 2, subtype)` + `cTn.setNodeType` + `addEffectsToMainSequence`). The person's editor
receives them, and the saved `p:timing` holds every effect with the right preset, subtype and node type.

Mapping used: fade → `fade` (entr 10); rise → fly-in subtype 4; drop → fly-in 1; left → fly-in 8; right → fly-in 2;
scale → `zoom` (entr 53, subtype 16 "in"); pop → `bounce` (entr 26; nearest PowerPoint preset, 2 s by default, set
`SetDuration`); exits: the same with class 2 (`fade-out`, fly-out 1/2/4/8, `zoom-out`).

Calls (`slide2/b1.js`, verbatim):
```js
var tl = s.GetTimeLine(), seq = tl.GetMainSequence(), T = tl.Timing;
var e1 = seq.AddEffect(shapes.fade, "fade", "onclick");
function fly(sh, presetClass, subtype, trigger) {      // presetClass 1 entrance, 2 exit; preset 2 = Fly In/Out
  var eff = T.createEffect(sh.Drawing.GetId(), presetClass, 2, subtype, null);
  eff.cTn.setNodeType(trigger === "afterprevious" ? AscFormat.NODE_TYPE_AFTEREFFECT : trigger === "withprevious" ? AscFormat.NODE_TYPE_WITHEFFECT : AscFormat.NODE_TYPE_CLICKEFFECT);
  T.addEffectsToMainSequence([eff]);
  return eff;
}
fly(shapes.rise, 1, 4, "afterprevious");          // from bottom, auto
fly(shapes.drop, 1, 1, "onclick");                // from top
fly(shapes.left, 1, 8, "onclick");                // from left
fly(shapes.right, 1, 2, "afterprevious");         // from right, auto
seq.AddEffect(shapes.scale, "zoom", "onclick");
seq.AddEffect(shapes.pop, "bounce", "onclick");
seq.AddEffect(shapes.exitFade, "fade-out", "onclick");
fly(shapes.exitRise, 2, 1, "afterprevious");      // exit: fly out to the top
```
Agent read-back (`GetAllEffects()`: shape, `GetEffectType`, `GetTriggerType`, subtype, `GetDuration` ms), and the
person's page 3 s later (`b1r.js`) gave the same list:
```
["b:fade","entranceFade","onclick",0,500] ["b:rise","entranceFlyIn","afterprevious",4,500] ["b:drop","entranceFlyIn","onclick",1,500]
["b:left","entranceFlyIn","onclick",8,500] ["b:right","entranceFlyIn","afterprevious",2,500] ["b:scale","entranceZoom","onclick",16,500]
["b:pop","entranceBounce","onclick",0,2000] ["b:exitFade","exitFadeOut","onclick",0,500] ["b:exitRise","exitFlyOut","afterprevious",1,500]
```
Saved (`saved/<key>-33.pptx`, slide8.xml `p:timing`, the effect `p:cTn` of each, matched to shapes by `spid`):
```
b:fade  presetID=10 presetClass=entr presetSubtype=0  nodeType=clickEffect   b:rise  presetID=2 entr subtype=4 afterEffect
b:drop  presetID=2  entr subtype=1 clickEffect                                b:left  presetID=2 entr subtype=8 clickEffect
b:right presetID=2  entr subtype=2 afterEffect                                b:scale presetID=53 entr subtype=16 clickEffect
b:pop   presetID=26 entr subtype=0 clickEffect                                b:exitFade presetID=10 exit clickEffect
b:exitRise presetID=2 exit subtype=1 afterEffect
<p:timing><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot">…<p:seq concurrent="1" nextAc="seek">
  <p:cTn id="2" dur="indefinite" nodeType="mainSeq">…<p:animEffect transition="in" filter="fade">… (no p:bldLst)
```
Gotchas:
- `AddEffect` passes subtype 0; `createEffect` then falls back to the preset's first subtype: a public `"fly-in"`
  came out as subtype **1** (from the top) (`slide2/b2.js`). Use the internal route for any direction.
- Effects reference shapes by drawing id: add them after `AddObject`, in the same group. `slide.RemoveAllObjects()`
  (the in-place rebuild) also empties the timeline (`b2.js`: 10 effects before, 0 after), so a rebuilt slide simply
  re-adds its builds.
- "pop" has no PowerPoint equivalent; `bounce` (2 s) or `zoom` are the nearest. Durations: Claude Slides' are not in
  format.md; `ApiAnimationEffect.SetDuration(ms)` sets them.
- No `p:bldLst` is written: fine for shapes; PowerPoint's by-paragraph text builds would need it (not in the format).

#### 12. Icons

**Answer:** a 48x48 single-colour SVG inserted with `Api.CreateImage("data:image/svg+xml;base64,…", w, h)` renders
as a sharp vector in the person's editor at 48 px and at 192 px, in its own colour (nothing recoloured), and is saved
as `svgBlip` + a PNG fallback. Two traps for the file: **the PNG fallback is rasterised at the SVG's own
`width`/`height`** (48x48 → blurry at 192 px in readers that use the fallback), and **the same SVG data URL used twice
in one command keeps the SVG only on the first picture** (the second saved with the PNG fallback alone).
runtime-office: write each icon's SVG with `width`/`height` = 2x its box and a per-picture nonce (a comment).

Calls (`slide2/i1.js`, `i2.js`; the SVG is a 24x24-viewBox star path, `fill="#C81E1E"`, `width="48" height="48"`):
```js
function pic(src, x, y, w, h) { var im = Api.CreateImage(src, w * E, h * E); im.SetPosition(x * E, y * E); return im; }
put(s, pic(scope.svg48, 60, 60, 48, 48), "icon48");
put(s, pic(scope.svg48, 160, 60, 192, 192), "icon48at192");           // same data URL, same command
put(s, pic(scope.svg192, 400, 60, 192, 192), "icon192at192");         // width/height = the box
// next command (i2.js): the same data URL again, and a copy made unique with "<!-- n1 -->"
put(s, pic(scope.svg48, 700, 60, 96, 96), "icon48again");
put(s, pic(scope.svg48b, 850, 60, 96, 96), "icon48unique");
```
Saved (`saved/<key>-35.pptx`, each picture's embeds):
```
icon48        ['../media/image1.png', '../media/media1.svg']  svgBlip   (image1.png is 48 x 48)
icon48at192   ['../media/image1.png']                          NO svgBlip (same command, same data URL)
icon192at192  ['../media/image2.png', '../media/media2.svg']  svgBlip   (image2.png is 192 x 192)
icon48again   ['../media/image4.png', '../media/media3.svg']  svgBlip   (a later command: uploaded again)
icon48unique  ['../media/image5.png', '../media/media4.svg']  svgBlip
```
The person's render (`slide2/shot-icons-person.png`): three crisp red stars. LibreOffice (`slide2/lo-icons.png`):
`icon48at192` blurry (it only has the 48 px PNG).

Gotchas:
- `GetClassType()` of these SVG pictures read `"image"` here; the first probe saw `"drawing"` for an SVG picture.
  The reader must accept both.

#### 13. Blend modes and filters

**Answer: pictures.** The OOXML forms exist in the model and are saved (`a:grayscl`, `a:lum` on a picture's blip,
`a:blur` in a shape's effect list; `CBlend`/`CFillOverlay`/`CDuotone`/`CHslEffect` classes exist) but **the editor
draws none of them**: the person sees the plain picture/shape. `mix-blend-mode` and `backdrop-filter` depend on
what lies underneath, which OOXML cannot express (`fillOverlay` blends with a fill, not with the backdrop). So
runtime-office rasterises the element in Chromium; for `mix-blend-mode`/`backdrop-filter` the picture must be the
composite of the element *and* its backdrop region (the native shapes below stay, the picture covers that region).

Calls (`slide2/i1.js`, verbatim):
```js
function withBlipEffect(im, eff) { var bf = im.Drawing.blipFill.createDuplicate(); bf.Effects.push(eff); im.Drawing.setBlipFill(bf); return im; }
put(s, withBlipEffect(pic(scope.png, 500, 400, 400, 300), new AscFormat.CGrayscl()), "grayscale");
var lum = new AscFormat.CLumEffect(); lum.bright = 30000; lum.contrast = 0;
put(s, withBlipEffect(pic(scope.png, 940, 400, 400, 300), lum), "brightness");
var lst = new AscFormat.CEffectLst(), b = new AscFormat.CBlur(); b.rad = 20 * E; lst.blur = b;
var pr = new AscFormat.CEffectProperties(); pr.EffectLst = lst; blurShape.Drawing.spPr.setEffectPr(pr);
```
Saved: `grayscale <a:grayscl/>`, `brightness <a:lum bright="30000" contrast="0">`, `blurShape <a:blur rad="127000">`.
The person's render (`slide2/shot-icons-person.png`, bottom row): the grayscale and brightness pictures look exactly
like the plain one, the blurred shape is sharp. LibreOffice draws grayscale and brightness (so the XML is right),
not the blur.

#### Summary table

"public" = Builder API; "internal" = an sdkjs object/method reached from a command (named in the section).

| Feature | Native call | Picture |
|---|---|---|
| Linear gradient, any angle, 2–8+ stops, rgba stops | `Api.CreateLinearGradientFill(stops, ((css−90) mod 360)·60000)` + internal `lin.scale=false` (true for corner keywords); stop alpha = internal `CColorMod("alpha")` | |
| Repeating linear gradient | expanded hard stops (same call) | |
| Radial gradient | (centre not settable; saved without `fillToRect`, other readers draw it from the corner) | **picture** |
| rgba / fill opacity | internal `CUniFill.transparent` (`Api.RGBA` alpha is dropped) | |
| Element `opacity` | alpha into fill, line, runs (exact when nothing overlaps) | container with children: picture |
| Slide / text-box background | `slide.SetBackground(fill)`, shape fill | |
| Rounded rect, pill | preset `roundRect`, `adj = min(r/min(w,h), .5)·1e5` | |
| Four different radii, elliptical corners | public custom geometry (`Api.CreateCustomGeometry`, `ArcTo`) | |
| ellipse, diamond | presets | |
| Arrows (head = last 40%) | `right/left/up/downArrow`, `adj1` = shaft, `adj2 = .4·len/min(w,h)·1e5` | |
| `<hr>`, line shape | `line` preset geometry on a rect (`SetGeometry`) + `SetSize(w, 0)` | |
| Stroke width, solid/dashed/dotted | `Api.CreateStroke(px·6350, fill, "dash"/"sysDot")`, geometry inset by w/2; dots: internal `cap=rnd` | |
| Stroke rgba | internal `ln.Fill.transparent` | |
| Double stroke | custom geometry: fill path + two stroke paths, width w/3 (`cmpd=dbl` is not drawn) | |
| Border on some sides | one filled rect per side (solid) / a line per side (dashed) | rounded + mixed sides |
| Connector straight, heads none/open/filled, dash, 2–32 px | line p:sp + `SetFlipH/V`; heads internal `CLn.headEnd/tailEnd` (`EndArrow`) | |
| Connector hv / vh / elbow | open custom-geometry polyline (or `bentConnector3`) | |
| box-shadow, blur 0 (offset, spread, rgba) | internal `spPr.setEffectPr(COuterShdw)` | |
| box-shadow with blur | (saved right, editor draws it hard) | **Chromium shadow picture grouped behind the native shape** |
| Inset shadow | (saved as `innerShdw`, drawn by nobody) | **picture** |
| text-shadow | blur 0: a second text box offset behind | blurred: shadow-only picture behind, grouped |
| Rotation (shape, text, picture) | `SetRotation(deg)` normalised to 0–360 | |
| Skew | painted box: parallelogram custom geometry | text / picture: **picture** |
| Flips | `SetFlipH/V` | |
| Letter spacing | `ApiRun.SetSpacing(px·10)` (twips) | |
| uppercase / lowercase / capitalize | `SetCaps(true)` or transform the text / transform the text | |
| Strikethrough, single underline | `SetStrikeout`, `SetUnderline` | |
| Double/dotted/dashed/wavy underline, underline colour | nearest: single underline | exact: **picture** |
| Outlined text (`-webkit-text-stroke`) | `ApiRun.SetOutLine(stroke)` | |
| Gradient text (`background-clip:text`) | `ApiRun.SetFill(gradient)` (not `SetTextFill`) | |
| Text opacity | `SetFill(solid)` + internal `transparent` | |
| `white-space:nowrap` | internal `bodyPr.wrap = nTWTNone` via `txBody.setBodyPr` | |
| `object-fit:contain` | size the picture | |
| `object-fit:cover`, `data-crop` | internal `blipFill.srcRect` via `setBlipFill` (new pictures only) | |
| Picture rounded corners | internal picture `spPr.setGeometry(roundRect)` | |
| Picture opacity | internal `CImageShape.setTransparent` (new pictures only) | |
| Table row background, % widths, header row | build3's per-cell `SetShd`, measured px widths, run formatting | |
| `hidden` | `slide.SetVisible(false)` | |
| Transition fade / push / magic | `CreateSlideShowTransition` + `SetEntryEffect("effectFadeSmoothly" / "effectPushLeft" / "effectMorphByObject")`; magic pairs named `!!id` via internal `Drawing.setName` | |
| Sections (`data-section`) | **none** (internal `addSection` breaks co-editing and saving) | (drop) |
| Builds fade / scale / pop, order, auto | `GetTimeLine().GetMainSequence().AddEffect(d, "fade"/"zoom"/"bounce", trigger)` | |
| Builds rise / drop / left / right (+ exits) | internal `Timing.createEffect(id, 1|2, 2, 4/1/8/2)` + `setNodeType` + `addEffectsToMainSequence` | |
| `<x-icon>` | SVG picture, `width/height` = 2x box, unique per picture | |
| `filter`, `backdrop-filter`, `mix-blend-mode` | (saved forms exist; editor draws none) | **picture** (blend/backdrop: composite with the backdrop) |
| `<svg>`, `<x-embed>` | (first probe: SVG pictures) | picture |

#### Open questions

- **PowerPoint itself was not available.** LibreOffice stood in as the third-party reader (it confirmed radial
  gradients from the corner, blurred shadows, text-box shadows on glyphs, grayscale/brightness). PowerPoint's
  rendering of `blurRad = CSS blur`, of `p159:morph` with `!!` names, and of the fly-in subtypes is assumed from the
  spec, not seen.
- **Claude Slides' own geometry for arrows, connector heads, build durations and "pop"** is not in format.md:
  shaft 50%, `med` heads, 500 ms and `bounce` are placeholders to calibrate against its renderer.
- **Baseline offset** (section 7): one measurement (64 px Arial) put the editor's first baseline ~2 px below
  Chromium's; not studied across fonts and sizes.
- **Why an un-recorded picture edit was later "dragged along"** to the person (section 8): observed, not explained;
  the design avoids it by never editing a picture's blipFill in place.
- **Sections**: whether a section added by the person in a desktop app survives an agent edit was not tried (the
  model keeps `Sections` from the file; the agent should simply never write them).
- Not exercised: several box-shadows on one element (OOXML takes one), elliptical `border-radius` values, gradient strokes.

## PDF

DEV document server 9.3.5 (`https://office.densemax.local/9.3.5-dev001/`), PDF editor (`documentType: "pdf"`, web-apps
`apps/pdfeditor`, sdkjs `word/sdk-all.js` + `pdf/src/engine/drawingfile.js`), opened through the probe (runtime-office's
`PagePool` + host page + connector), config `permissions {edit, review, comment, fillForms: true}`, `mode: "edit"`,
`coEditing {mode: "fast"}`, `customization {forcesave: true, plugins: false}`. Fixture `fixtures/sample.pdf` (2 pages,
DejaVuSans-Bold + DejaVuSerif subsets, AcroForm text field `client` and checkbox `signed`). Scripts in `pdf/`.

`PC` below means `PC_PORT=8794 python3 pc.py`. (Written as I go; sections fill in below.)

### Probe changes I had to make (my own copies, nothing shared touched)

- `probe-pdf.mjs` / `start-probe-pdf.sh`: copies of `probe.mjs` / `start-probe.sh` with (1) `/eval` that also reaches a page the
  pool counts as dead (`pool.pages.get(id)`), for diagnosis; (2) `/shot {id, name}` (page screenshot to `saved/<name>.png`);
  (3) a request log line per non-command request. Without (1), one timed-out method makes the pool close the page and the
  evidence goes with it.
- `pdf/open.sh <fixture> [key] [user-id user-name]`: open with one retry.
- `pdf/hook.js` (run with `eval <id> … frame` right after open): records `console.error/warn`, window `error` events and
  `asc_onError` into the frame's `window.__log`. This is how every crash below was seen.
- `pdf/forcesave.py <key> [userdata]`: Command Service forcesave, HS256 JWT in the body (secret read from
  `/work/surogates/config.dev.yaml`, never printed).
- **DEV gotcha:** after my probe restarted on the same port, every open of `sample.pdf` failed with
  `{"errorCode":-4,"errorDescription":"Download failed."}` although the document server fetched `/files/sample.pdf` (200)
  and `sample.xlsx` opened fine. A copy under a new name (`pdfspike.pdf`, `pdfspike2.pdf`, …) opened at once. Looks like the
  document server caches a failed download per URL. The runtime serves each document at a stable URL: a transient failure
  there could stick. (Not investigated further.)

---

### Verify item 12: does text edited through `RecognizeContent` keep the original embedded fonts?

**Answer: kept, with substitution only for missing glyphs.** The saved PDF draws the edited text with the *original font
object* (`AAAAAA+DejaVuSerif`, the same FontFile2 stream, obj 23/21, same 96 glyphs), in its original single-byte encoding.
A character the embedded subset has no glyph for (`ü`) is drawn from a **new embedded subset of Arial**
(`BAAAAA+Arial`, CID TrueType, Identity-H, embedded) — not lost, not tofu. pdftotext has every new word.

**But the op is only usable with two fixes, both tested:**
1. `RecognizeContent` from a connector command **crashes the editor** unless the command is sent with `recalculate: false`
   (runtime-office's host page hard-codes `recalculate: true`).
2. A recognized block is a **fixed-size, clipped text box**: text that grows past its height is cut off in the saved PDF
   (still in the text layer, invisible on the page). Grow the box after the edit
   (`drawing.private_GetImpl().checkExtentsByDocContent(true)`, internal) or the edit silently loses lines.

Also: recognition merges a block's lines into **one paragraph** ("The supplier … 2026." and "Fees are due …" became one
paragraph in one shape), so any length change **re-wraps the whole block** to the box width: the original line breaks are
not kept. Unedited recognized shapes on the page re-render identically.

#### What `RecognizeContent` is

`ApiPage.prototype.RecognizeContent = function(){ … oDoc.EditPage(this.GetIndex()); return AscBuilder.GetApiDrawings(
this.Page.drawings.slice(nCurLength)) }` (sdkjs `word/sdk-all.js`, PDF builder API). It converts the whole page's text into
shapes (`ApiShape`, a Word `ApiDocumentContent` inside), returns them, and marks the page recognized (a second call returns
nothing: `EditPage` returns false when `isRecognized`). On save the page's original text drawing is replaced by those
shapes (each emitted as `/OShapes` marked content, clipped to the shape's rect).

#### Proof

Recognize, sent with `recalculate: false` straight through the host page's connector (`pdf/recog-norecalc.txt`, run with
`eval <id> "<it>"` in the host page):
```js
(async () => {
  const src = "function () { try { var g = Api.GetDocument().GetPage(0).RecognizeContent(); return g.length; } catch (e) { return { __error: String(e && e.stack || e) }; } }";
  const code = "var Asc = {}; Asc.scope = {}; var scope = Asc.scope; (" + src + ")();";
  return conn.send({ type: "command", guid: conn.guid, data: code, recalculate: false }, 30000);
})()
// -> {"sentAt":…, "value": 3}; frame log empty, isLongAction() false, canSave true
```
The shapes and their runs (`pdf/runs.js`): fonts are the embedded fonts, by a synthetic name.
```
shape 0: [run 'Service agreement'  font "Embedded: AAAAAA+DejaVuSans-Bold C503DD59B1FCD5B4E836528E44F2B8E6" size 40 (half-points) bold]
shape 1: [run 'The supplier delivers the service from 1 March 2026. ', run 'Fees are due within thirty days of each invoice.'
          both font "Embedded: AAAAAA+DejaVuSerif 418291FBDE5F6387CAAE81DC568745D1" size 24]
shape 2: [run 'Client name: ', run 'Signed:'  same DejaVuSerif]
positions (EMU, from the page's top-left): shape 1 x 914400 (72 pt) y 1228342 w 4125366 h 457199
```
The edit (`pdf/runreplace.js`), run-level so the run keeps its font (`ApiRun.ClearContent` + `AddText`):
```js
function () {
  try {
    var shape = Api.GetDocument().GetPage(scope.page).GetAllDrawings()[scope.shape], content = shape.GetContent(), out = [];
    scope.edits.forEach(function (ed) {
      for (var k = 0; k < content.GetElementsCount(); k++) {
        var p = content.GetElement(k);
        for (var i = 0; i < p.GetElementsCount(); i++) {
          var r = p.GetElement(i), t = r.GetText();
          if (t.indexOf(ed.find) === -1) continue;
          r.ClearContent();
          r.AddText(t.replace(ed.find, ed.text));
          out.push({ find: ed.find, para: k, run: i });
          return;
        }
      }
      out.push({ find: ed.find, missing: true });
    });
    /* … returns the runs … */
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
// scope {"page":0,"shape":1,"edits":[{"find":"thirty","text":"ninety"},{"find":"1 March","text":"1 Zürich-quick"}]}
// -> runs ["The supplier delivers the service from 1 Zürich-quick 2026. ", "Embedded: AAAAAA+DejaVuSerif 4182…"],
//         ["Fees are due within ninety days of each invoice.", "Embedded: AAAAAA+DejaVuSerif 4182…"]
```
(The first try, sent 180 ms after the recognize, came back `undefined`: the editor skips commands right after a change;
the runtime's retry covers it.) Flush: `onDocumentStateChange` true at +77 ms, false at +1052 ms. Forcesave
(`pdf/forcesave.py <key> recognize-fonts`) -> `{"error":0}`, status-6 callback `forcesavetype 0`, `userdata
"recognize-fonts"`, file `saved/e211f22886e8dda1d7a5a0a7ef8c9f0c-12.pdf`.

`pdffonts` before / after:
```
before: Helvetica Type1 no-emb | AAAAAA+DejaVuSans-Bold TrueType WinAnsi emb sub (obj 19) | AAAAAA+DejaVuSerif TrueType WinAnsi emb sub (obj 23)
after:  Helvetica Type1 no-emb | AAAAAA+DejaVuSans-Bold TrueType WinAnsi emb sub (obj 19) | AAAAAA+DejaVuSerif TrueType WinAnsi emb sub (obj 23)
        | BAAAAA+Arial  CID TrueType  Identity-H  emb sub (obj 36)
```
Which font draws which text (`pdf/fontuse.py`, pypdf text visitor) and the glyphs in each font program (`pdf/glyphs.py`,
fontTools):
```
after, page 1: /F2 -> obj 19, /F3 -> obj 23 (the same objects as the original /F2+0, /F3+0), /F4 -> obj 36 Arial
   /AAAAAA+DejaVuSans-Bold: Service agreement
   /AAAAAA+DejaVuSerif: The supplier delivers the service from 1 Z | rich-quick | 2026. Fees are due within ninety days of each | invoice. | Client name: | Signed:
   /BAAAAA+Arial: ü
DejaVuSerif FontFile2: 96 glyphs before, the same 96 after (reportlab's subset holds all of printable ASCII, so Z q k - were in it);
Arial FontFile2: a new subset.
content stream: "/F3 12 Tf … [<…5A>]TJ  /F4 12 Tf <0001> Tj  /F3 12 Tf [<726963682D>…<717569636B>]TJ" — ü alone switches font.
```
Rendered (`pdftoppm -r 80`): the new words look right (DejaVu Serif; the Arial ü is close), **but the third line
"invoice." is not on the page**: the content stream draws it below the shape's clip path
(`-4.53543 846.4252 m … 805.88986 l … W n`, 40.5 pt tall; the line is at -36 pt). Shape height stayed 457199 EMU while its
content is 19.04 mm (`txBody.content.GetSummaryHeight()`); text-fit is `spAutoFit` (1) but nothing resizes it after a
builder edit.

The fix, tested: after the edit,
```js
function () { try { var d = Api.GetDocument().GetPage(0).GetAllDrawings()[1], sp = d.private_GetImpl();
  var before = d.GetHeight(); var r = sp.checkExtentsByDocContent(true); return { before: before, ret: String(r), after: d.GetHeight() };
} catch (e) { return { __error: String(e && e.stack || e) }; } }
// -> {"before":457199,"ret":"true","after":685779.0004317601}
```
forcesave again (`saved/e211…-13.pdf`): all three lines rendered. The box grows downward: on a dense page it will overlap
what is below; the op must refuse (or warn) when the grown box intersects another block.

#### The crash, and why (`recalculate: true`)

With the runtime's normal command (`pc.py cmd <id> 'function () { … RecognizeContent() … }'`, `recalculate: true`), the
command returns 3, then the frame logs, 500 ms later:
```
changesError: Error: Uncaught TypeError: this.pasteCallback is not a function
  at PDFEditorApi.asyncImagesDocumentEndLoaded (sdk-all-min.js) … at asc_docs_api.asyncFontsDocumentEndLoaded
asc_onError -25 0   (critical)
```
The editor shows "An error occurred during the work with the document", goes `isViewMode` true, `isLongAction()` stuck
true, every later command answers `undefined`, and the document server sends status 2 with action type 0 (the agent
disconnected) 2.6 s later. Cause: `CPDFDoc.EditPage` loads the embedded fonts through `Asc.editor.pre_Paste(fonts, images,
fEndCallback)`, which is asynchronous; then the plugin manager's `_afterEvalCommand` (run only when `recalculate === true`
and the last history point is not empty) calls `AscCommon.Check_LoadingDataBeforePrepaste` -> `pre_Paste` again, which
finds nothing to load, calls its own callback at once and sets `pasteCallback = null`. When the first font load ends,
`pasteCallback` is null. `EditPage`'s own callback (`FinalizeAction`, `canSave = true`) never runs.
The UI's own path, `editor.asc_EditPage()` evaluated in the frame (it does `StartAction(Pdf_EditPage)` first, and runs
outside a command), recognized the page with no error (`{"log":[],"la":false,"vm":false,"canSave":true}`).
**Runtime change:** `host.ts runCommand` must take `recalculate` per command (false for the recognize command).

#### Item 12, second fixture: a real (tight) subset, and recognition fidelity

`fixtures/tight.pdf` (made by `pdf/make_tight.mjs`: Chromium `page.pdf()`, so fonts are subset to the glyphs used, like a
browser or Word export): `BAAAAA+DejaVuSerif` CID TrueType Identity-H, cmap chars `' .0126FMTacdefhilmnoprstuvwy'` (no
`Z g x`). Recognize (`recalculate: false`), then `{"edits":[{"find":"thirty","text":"sixty"},{"find":"March","text":"Zug"}]}`
with `pdf/runreplace.js`, forcesave -> `saved/b68c39a0471880f2c0a08ffd19d96ed4-70.pdf`:
```
pdffonts: AAAAAA+DejaVuSans-Bold (obj 4) | BAAAAA+DejaVuSerif (obj 5) | BAAAAA+Arial CID TrueType Identity-H emb sub (obj 24, new)
fontuse:  /BAAAAA+DejaVuSerif: The supplier delivers the service from 1 | u | 2026. Fees | are due within si | ty days of each invoice.
          /BAAAAA+Arial: Z | g | x
glyphs:   DejaVuSerif FontFile2 obj 13: the same 93 glyphs, same cmap as the original
```
**So the rule holds per glyph:** a glyph the original subset has is drawn with the original embedded font object; a glyph it
lacks comes from an embedded Arial subset — mid-word ("si" DejaVu, "x" Arial, "ty" DejaVu). Visible in the render
(`pdf/tight-edit-1.png`): "Zug" and the x of "sixty" are sans-serif.

**Recognition is not faithful on this file, even unedited** (recognize only, forcesave `saved/577478…-72.pdf`,
`pdf/tight-reconly-1.png`): word positions are kept (pdftotext -bbox: "Service" x 54.00–137.11 before, 54.00–136.50 after)
but the runs come back a point smaller (title 38 half-points = 19 pt for 20 pt; body 22 = 11 pt for 12 pt) and are drawn
letter-spaced to keep the words' extents. After an edit, the merged paragraph re-wraps and the second run's recognized
character spacing (`GetSpacing()` -13, i.e. -0.65 pt) goes with it: the line "are due within sixty days of each invoice." is
visibly condensed (`pdf/tight-edit-1.png`). The reportlab fixture (integer sizes, no text matrix scaling) came back exact
(24 / 40 half-points, spacing 0).

**What this means for `replace`:** fonts are kept, but `RecognizeContent` rewrites the *whole page* as text boxes on save:
every block on the page can shift in size/spacing by recognition alone, an edited block re-wraps, may clip (fix above), and
mixes in Arial for glyphs the subset lacks. It is a "retype this page" op, not a surgical one. Gate it: recognize only the
page being edited, warn that the page is re-laid out, prefer replacements whose glyphs are in the subset (the shape's runs
name the embedded font; the subset's coverage is not readable through the API — open question).

#### How `replace` should run (from what worked)

1. Its own command, sent with `recalculate: false`: `Api.GetDocument().GetPage(i).RecognizeContent()` (skip when
   `page.Page.IsRecognized()`, internal; a second call returns `[]`). Do not edit in the same command: `EditPage` finishes
   asynchronously (its callback calls `FinalizeAction()` when the embedded fonts have loaded) and, when nothing needs
   loading, that callback runs synchronously in the middle of the command.
2. The next command (normal `recalculate: true`; the first try may come back `undefined`: retry): find the run whose text
   contains the words in `page.GetAllDrawings()` shapes -> paragraphs -> runs, `ClearContent()` + `AddText(newText)` (the
   run keeps its font and size), then `shape.private_GetImpl().checkExtentsByDocContent(true)` so nothing is clipped.
   ApiRange does not work here (`ApiRange.GetText` -> `Document.SaveDocumentState is not a function`), so words that
   span two runs (e.g. "2026. Fees" across the recognized lines) need the runs joined by hand: not built.
3. Flush, forcesave as usual.

---

### Q1. Enumeration; is the editor in edit mode?

**Answer: the page opens in the PDF editor's "Commenting" mode (`restrictions` 128 = View, `isRestrictionView()` true,
`canEdit()` false), and every builder edit below still works through connector commands in that mode. No config change is
needed.** The PDF editor has no config for starting in "Edit PDF" mode: web-apps sets `appOptions.isPDFEdit = false` at load
and only the toolbar's mode switch (`Common.NotificationCenter.trigger("pdf:mode-apply","edit")`, internal) turns it on.
With `permissions.edit` true, `canPDFEdit`/`canPDFAnnotate`/`canSwitchMode` are all true (web-apps `app.js`).

`pdf/enum1.js`, `pdf/enum2.js`, `pdf/enum3.js` (the `methods()` of `enumlib.js`):
```
Api (143): AddComment … CreateCaretAnnot CreateCheckBoxForm CreateCheckboxField CreateCircleAnnot CreateComboboxField CreateDateField
  CreateFreeTextAnnot CreateHighlightAnnot CreateImageField CreateInkAnnot CreateLineAnnot CreateListboxField CreatePolyLineAnnot
  CreatePolygonAnnot CreateRadiobuttonField CreateRedactAnnot CreateRichParagraph CreateRichRun CreateRichTextPr CreateShape
  CreateSquareAnnot CreateStampAnnot CreateStrikeoutAnnot CreateTextAnnot CreateTextField CreateUnderlineAnnot GetByInternalId
  GetDocument … (plus the Word builder's Create*/conversion helpers)
document ("document"): AddPage ApplyRedact GetAllFields GetClassType GetFieldByName GetPage GetPagesCount RemovePage SearchAndRedact
page ("page"): AddObject GetAllAnnots GetAllDrawings GetAllWidgets GetClassType GetIndex GetRotation GetSelectedText
  GetSelectionQuads RecognizeContent Search SetRotation SetSelection
textField: AddWidget ClearFormat Delete GetAllWidgets GetCharLimit GetClassType GetFullName GetPartialName GetValue IsComb
  IsMultiline IsReadOnly IsRequired IsScrollLongText SetCharLimit SetComb SetDateFormat SetFullName SetMask SetMultiline
  SetNumberFormat SetPartialName SetPercentageFormat SetReadOnly SetRegularExp SetRequired SetScrollLongText SetSpecialFormat
  SetTimeFormat SetValidateRange SetValue
checkboxField: AddOption AddWidget Delete GetAllWidgets GetClassType GetFullName GetPartialName GetValue IsReadOnly IsRequired
  IsToggleToOff SetFullName SetPartialName SetReadOnly SetRequired SetToggleToOff SetValue
textWidget: Delete Get/SetBackgroundColor Get/SetBorderColor Get/SetBorderStyle Get/SetBorderWidth GetClassType Get/SetPlaceholder
  Get/SetPosition Get/SetRect Get/SetRegularExp Get/SetTextColor Get/SetTextSize IsAutoFit SetAutoFit
checkboxWidget: (the same) + Get/SetCheckStyle Get/SetExportValue IsChecked SetChecked IsCheckedByDefault SetCheckedByDefault
annotation (all): Get/SetRect Get/SetPosition Get/SetAuthorName Get/SetContents Get/SetCreationDate Get/SetModDate
  Get/SetUniqueName Get/SetOpacity Get/SetSubject Get/SetDisplay Get/SetBorder* Get/SetFillColor AddReply GetReplies Delete;
  freeTextAnnot + GetContent (ApiRichContent) Get/SetIntent Get/SetCallout Get/SetRectDiff; stampAnnot + GetType Get/SetScale
  Get/SetRotation; redactAnnot/markup + Get/SetQuads
shape (a recognized text block): Copy Delete GetClassType GetContent (Word ApiDocumentContent) GetFill GetGeometry GetHeight GetLine
  GetParentPage GetPosX GetPosY GetRotation GetWidth Select SetFill SetGeometry SetLine SetPosX SetPosY SetPosition SetRotation
  SetSize SetVerticalTextAlign; its paragraphs and runs are the Word builder's ApiParagraph/ApiRun.
```
The source of the whole PDF builder API is readable in `https://office.densemax.local/9.3.5-dev001/sdkjs/word/sdk-all.js`
(not the -min file); `pdf/ds/pdfapi.js` is that region split one method per line.

Mode, read in the frame right after open: `{"getViewMode":false,"isRestrictionView":true,"isRestrictionForms":false,
"canEdit":false,"isPdfEditor":true,"restrictions":128,"isViewMode":false}`. A command in that state:
`function () { … var p = Api.GetDocument().GetPage(1); … p.SetRotation(90) … }` -> `{"before":0,"ret":true,"after":90}`;
the saved file has `/Rotate 90` on page 2.

**Gotchas (Q1):**
- **Every command, even a read (`GetPagesCount()`), fires `onDocumentStateChange` true and, ~1 s later, false** in the PDF
  editor (Word fires nothing for a read). The flag `isDocumentModified()` is therefore true right after any command.
- **An uncaught exception anywhere in the editor's frame is fatal:** the editor's `window.onerror` turns it into
  `asc_onError -25` (critical): "An error occurred during the work with the document", view mode, disconnected. My own
  `eval … frame` of a `setTimeout` callback that threw (`window.events` is undefined in the frame) killed a page this way.
  Frame-evaluated code must never throw asynchronously.
- `private_CheckPoint` rejects a coordinate of 0 (`GetNumberParameter(0, null) || throw`): use 0.01.

---

### Q2. Reading

**Answer:** page count `doc.GetPagesCount()`; page text has **no direct API**: select the whole page and read the selection
(`page.SetSelection({x:0.01,y:0.01},{x:100000,y:100000}); page.GetSelectedText()`), except on a **recognized page, whose
text layer is the original file's (stale)**: read its shapes instead. Fields: `page.GetAllWidgets()` (rect, type) +
`widget.Field` (internal) for the name and value. Annotations: `page.GetAllAnnots()`. Search: `page.Search({text,
matchCase})` returns quads on that page; it searches the *current* content (recognized shapes included, stale layer not).

`pdf/outline.js` (outline, and with `{"view": n}` one page's full text, clipped to 30,000 characters):
```js
function () {
  try {
    var doc = Api.GetDocument(), pdf = doc.Document, pages = [];
    function pageText(page) {
      // A recognized page's text layer is the original file's: its text is in its shapes.
      if (page.Page.IsRecognized && page.Page.IsRecognized()) {
        return page.GetAllDrawings().filter(function (d) { return d.GetClassType() === "shape"; }).map(function (d) {
          var c = d.GetContent(), lines = [];
          for (var k = 0; k < c.GetElementsCount(); k++) { var el = c.GetElement(k); if (el.GetText) lines.push(String(el.GetText()).replace(/\r?\n$/, "")); }
          return lines.join("\n");
        }).join("\n");
      }
      page.SetSelection({ x: 0.01, y: 0.01 }, { x: 100000, y: 100000 });
      return String(page.GetSelectedText() || "");
    }
    for (var i = 0; i < doc.GetPagesCount(); i++) {
      var page = doc.GetPage(i), file = pdf.GetFile().pages[page.GetIndex()], text = pageText(page);
      pages.push({
        page: i + 1, w: file.W, h: file.H, rotation: page.GetRotation(),
        first_line: (text.split("\n").filter(function (l) { return l.trim(); })[0] || ""), chars: text.length,
        fields: page.GetAllWidgets().map(function (w) {
          var f = w.Field;
          return { name: f.GetFullName(), type: w.GetClassType(), value: f.GetParentValue ? f.GetParentValue() : null, rect: w.GetRect() };
        }),
        annots: page.GetAllAnnots().map(function (a) {
          return { type: a.GetClassType(), rect: a.GetRect(), author: a.GetAuthorName(), text: a.GetContents() };
        })
      });
    }
    if (scope.view) return { page: scope.view, text: pageText(doc.GetPage(scope.view - 1)).slice(0, 30000) };
    return { pages: pages };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
```
On the page where page 1 was recognized and edited (`e211…`):
```
{"pages":[{"page":1,"w":595,"h":842,"rotation":0,"first_line":"Service agreement","chars":147,
   "fields":[{"name":"client","type":"textWidget","rect":[170,169.8898,420,189.8898]},
             {"name":"signed","type":"checkboxWidget","value":"Off","rect":[170,210.8898,186,226.8898]}],"annots":[]},
  {"page":2,"w":595,"h":842,"rotation":0,"first_line":"Page two: the schedule of services.","chars":36,"fields":[],"annots":[]}]}
view 1 -> "Service agreement\nThe supplier delivers the service from 1 Zürich-quick 2026. Fees are due within ninety days of each invoice.\nClient name: Signed:"
```
The selection method on the same recognized page returns the **old** text (`pdf/pagetext.js`):
`"Service agreement\nThe supplier delivers the service from 1 March 2026.\nFees are due within thirty days of each invoice.\nClient name:\nSigned:\n"`
(page 2, not recognized: `"Page two: the schedule of services.\n"`).

Search (`pdf/search.js`, `{"words":["thirty","ninety","services","Zürich"]}`):
```
thirty: []   (gone after the edit)
ninety: [{page:0, quads:[[232.57,114.71, 270.57,114.71, 232.57,132.70, 270.57,132.70]]}]
services: [{page:1, quads:[[232.82,70.75, 282.41,70.75, 232.82,84.72, 282.41,84.72]]}]
Zürich: [{page:0, quads:[[315.85,96.72, 354.89,96.72, 315.85,114.71, 354.89,114.71]]}]
recognized: page 0 Page.IsRecognized() true, its 3 drawings private_GetImpl().IsFromScan() true; page 1 no drawings
```
A quad is 8 numbers: top-left, top-right, bottom-left, bottom-right, in points from the page's top-left.

**Gotchas (Q2):**
- `page.Search` runs a whole-document search each call and keeps that page's matches: one call per page is O(pages²).
  Fine for tens of pages; for a long PDF run one `page.Search` and group by `pdfMatch.GetAbsolutePage()` internally, or cap.
- `file.W/H` (`doc.Document.GetFile().pages[i]`, internal) are rounded (595 × 842 for 595.28 × 841.89). Good enough to
  convert; the mediabox is exact in the saved file.
- `widget.Field` and `page.Page.IsRecognized()` are internal (no public widget-to-field or "is recognized" call). A text
  field with no value has `GetParentValue()` undefined.
- Selection reads change the agent's own selection only (not shared); search results highlight in the agent's view only.

---

### Q3. Pages

**Answer:** `doc.AddPage(index[, wPt, hPt])` (blank, the neighbour's size), `doc.RemovePage(index)` (false when no such
page), `page.SetRotation(deg)` (absolute, multiples of 90 only; 45 -> false). All three survive the save.

`pdf/pages.js`:
```js
var doc = Api.GetDocument(), out = { before: doc.GetPagesCount() };
var added = doc.AddPage(1);              out.addedIndex = added.GetIndex(); out.afterAdd = doc.GetPagesCount();
out.removed = doc.RemovePage(2);         out.afterRemove = doc.GetPagesCount();
out.rotated = doc.GetPage(1).SetRotation(90);
out.rotations = [0, 1].map(function (i) { return doc.GetPage(i).GetRotation(); });
out.badRotation = doc.GetPage(1).SetRotation(45);  out.removeMissing = doc.RemovePage(9);
// -> {"before":2,"addedIndex":1,"afterAdd":3,"removed":true,"afterRemove":2,"rotated":true,"rotations":[0,90],"badRotation":false,"removeMissing":false}
```
Saved (`saved/e211…-26.pdf`, pypdf): 2 pages, `/Rotate` `[0, 90]`, mediaboxes `[0 0 595.27557 841.88977]`,
`[0 0 595 842]` (the new page takes the neighbour's rounded size).

---

### Q4. Forms

**Answer:** fill with `doc.GetFieldByName(name).SetValue(text)`; tick with `field.GetAllWidgets()[0].SetChecked(true)`
(its export value is `"Yes"` here); add with `Api.CreateTextField(rect)` / `Api.CreateCheckboxField(rect)`, then
`SetFullName(name)`, then `page.AddObject(field)`. **All values survive the save** (pypdf reads them).

`pdf/forms.js`:
```js
var doc = Api.GetDocument(), out = {};
out.setClient = doc.GetFieldByName("client").SetValue("ACME Lisbon Lda");
var signed = doc.GetFieldByName("signed");
out.signedWidgets = signed.GetAllWidgets().map(function (w) { return w.GetExportValue(); });
out.setChecked = signed.GetAllWidgets()[0].SetChecked(true);
var po = Api.CreateTextField([170, 250, 420, 270]); po.SetFullName("po_number"); doc.GetPage(0).AddObject(po); po.SetValue("PO-4471");
var ok = Api.CreateCheckboxField([170, 290, 186, 306]); ok.SetFullName("approved"); doc.GetPage(0).AddObject(ok);
ok.GetAllWidgets()[0].SetChecked(true);
out.after = doc.GetAllFields().map(function (f) { return { name: f.GetFullName(), cls: f.GetClassType(), value: f.GetValue() }; });
// -> setClient true, signedWidgets ["Yes"], setChecked true,
//    after [client textField "ACME Lisbon Lda", signed checkboxField "Yes", po_number textField "PO-4471", approved checkboxField "Yes"]
```
Saved (`saved/e211…-25.pdf`), pypdf `get_fields()` and the page's widgets:
```
client /Tx 'ACME Lisbon Lda' | signed /Btn '/Yes' | po_number /Tx 'PO-4471' | approved /Btn '/Yes'
/Widget client [170.0, 652.0, 420.0, 672.0] | signed [170.0, 615.0, 186.0, 631.0] /AS /Yes
/Widget po_number [170.0, 571.89, 420.0, 591.89] | approved [170.0, 535.89, 186.0, 551.89] /AS /Yes
```
**Gotchas (Q4):** `GetFieldByName` of a missing name throws (`this.Document.GetField(sName)` is null -> `.IsWidget` of
null): check `GetAllFields()` names first. `SetValue` on a checkbox field takes its export value; `SetChecked` on the widget
is simpler. A new field is named by `SetFullName` before `AddObject` (otherwise it gets `Text1`-style names).

---

### Q5. Redaction

**Answer:** `doc.SearchAndRedact({text, matchCase, wholeWords})` adds a **Redact annotation per match** (a mark: the text is
still there, the person sees the mark, and a save keeps it as an unapplied `/Redact` annotation). `doc.ApplyRedact()`
applies **every** unapplied redact mark in the document **immediately, in the editor** (text removed, black box drawn), and
the next save carries it: the phrase is gone from pdftotext. Cancel before applying: `mark.Delete()` on each returned
annotation. Cancel after applying, before saving: the `Undo` method (one command = one history point) brings the text back.
So "applied on save" is the runtime's choice: mark now, `ApplyRedact()` right before the forcesave.

`pdf/redact1.js` (`{"text":"supplier","cancel":true}` then `{"text":"thirty"}`):
```js
var marks = doc.SearchAndRedact({ text: scope.text, matchCase: false });
out.marks = marks.map(function (a) { return { cls: a.GetClassType(), rect: a.GetRect(), quads: a.GetQuads(), page: a.private_GetImpl().GetPage() }; });
if (scope.cancel) { marks.forEach(function (a) { a.Delete(); }); out.afterCancel = doc.GetPage(0).GetAllAnnots().map(…); }
// supplier: marks [{redactAnnot, rect [97.65,99.75,149.41,115.72], page 0}], annotsPage0 ["redactAnnot"], afterCancel []
// thirty:   marks [{redactAnnot, rect [195.67,117.75,231.40,133.72], page 0}]
```
Saved with the mark only (`saved/ebbd…-42.pdf`): pdftotext still has "Fees are due within thirty days"; annotations
`/Redact [195.7, 708.2, 231.4, 724.1] /T Agent2` (+ its `/Popup`).
Then `function () { … return { applied: doc.ApplyRedact(), annots: … } }` -> `{"applied":true,"annots":[]}`; a second
`ApplyRedact()` throws `"Has no redact to apply"`. Saved (`saved/ebbd…-43.pdf`): pdftotext `"Fees are due within\ndays of
each invoice."`, no `/Redact` left, render shows a black box where the word was; fonts unchanged.
Undo: `SearchAndRedact("supplier") + ApplyRedact()` in one command -> `Search` finds 0; `method CanUndo` -> true;
`method Undo`; `Search` finds 1 again and no annotation is left.

**Gotchas (Q5):** `ApplyRedact()` applies the person's pending marks too (it takes every redact annotation without a
redact id), and throws when there are none. On a **recognized page** the mark lands on the shape's text and the saved
file is redacted (pdftotext: "Fees are due within\ndays of each invoice.", black box), but **the live shape still holds the
words** (`Search` still finds "forty-five" after `ApplyRedact`; the shape's paragraph text unchanged): a later read shows
text the file no longer has. Until the save, an applied redaction is only in the editor session: an Undo by the
agent brings the text back.

---

### Q6. Annotations

**Answer:** stamp: `page.AddObject(Api.CreateStampAnnot(rect, "SB_Approved", author))` works as is (it keeps its own
aspect ratio: the rect is adjusted). Free text: `Api.CreateFreeTextAnnot(rect)`, text through a rich run, **plus an
internal commit** (`impl.SetRichContents(impl.GetRichContents(true))`), or the saved file has an **empty box**: the editor
commits a free text's rich text only when its text box loses focus (`CAnnotationFreeText.Blur`), which a builder edit never
does. The author is the page user's name unless `SetAuthorName` is called (the stamp's `author` argument only goes into
the stamp's picture).

`pdf/freetext2.js` (the version that saves correctly):
```js
function () {
  try {
    var page = Api.GetDocument().GetPage(0);
    var ft = Api.CreateFreeTextAnnot(scope.rect);
    var run = Api.CreateRichRun();
    run.AddText(scope.text);
    ft.GetContent().GetElement(0).AddElement(run, 0);
    ft.SetAuthorName(scope.author);
    page.AddObject(ft);
    // The editor commits a free text's rich text only when its text box loses focus (Blur); a builder edit never does.
    var impl = ft.private_GetImpl();
    impl.SetRichContents(impl.GetRichContents(true));
    ft.SetContents(scope.text);
    return { rc: JSON.stringify(impl.GetRichContents()).slice(0, 300), rect: ft.GetRect() };
  } catch (e) { return { __error: String(e && e.stack || e) }; }
}
// {"rect":[60,560,280,600],"text":"Committed rich text.","author":"Agent"}
// -> rc [{"size":10,"color":[0,0,0],"text":"Committed rich text.","actual":"Arial",…},{"text":"\r",…}]
```
Stamp (`pdf/annots.js`): `Api.CreateStampAnnot([300, 360, 450, 410], "SB_Approved", "Agent")` + `page.AddObject(st)` ->
rect `[300.47, 360, 467.72, 399.69]`, author `"Agent2"` (the page user). Types (`AscPDF.STAMP_TYPES`): D_Approved
D_Revised D_Reviewed D_Received SB_Approved SB_NotApproved SB_Revised SB_Confidential SB_ForComment SB_ForPublicRelease
SB_NotForPublicRelease SB_PreliminaryResults SB_InformationOnly SB_Draft SB_Completed SB_Final SB_Void SH_SignHere
SH_Witness SH_InitialHere Expired Image.
Saved (`saved/ebbd…-46.pdf`, pypdf): `/Stamp [299.0, 440.7, 469.2, 483.4] /T Agent2 /Name /#SB_Approved` (renders "APPROVED");
the committed `/FreeText [60, 242, 280, 282] /T Agent`, appearance stream draws the text (renders "Committed rich text.").
The two free texts made without the commit: `/RC` holds only `&#xD;`, the appearance stream has the border and no text
(`pdftoppm`: empty box; "Syntax Warning: Bad appearance for annotation"), although the agent's own editor shows the text
(screenshot `saved/R-agent.png`).

**Gotchas (Q6):** `ApiRichParagraph.AddText` throws in the PDF editor (`this.GetLastRunWithText is not a function`): use
`Api.CreateRichRun()` + `run.AddText` + `paragraph.AddElement(run, 0)`. A throw half-way leaves the half-made annotation
behind: the `Undo` method removed it (tested). Saved `/Contents` of a free text stays empty (the text is in `/RC`).

---

### Q7. Coordinates

**Answer: points (1/72 in) from the page's top-left, y down,** for annotation and widget rects, quads, `SetSelection`
points and `AddPage` sizes; rects are `[x1, y1, x2, y2]` with `x1 < x2`, `y1 < y2` (top < bottom). Recognized shapes
(`GetPosX/GetPosY/GetWidth/GetHeight`) are in **EMU** (12700 per point), same origin.
**Convert:** `pdf_x = x`, `pdf_y = pageHeight − y` (so a PDF `/Rect [llx lly urx ury]` is `[x1, H − y2, x2, H − y1]`).

Tested against known spots:
- reportlab drew the `client` field at `x=170, y=652, w=250, h=20` (PDF, bottom-left) on an 841.89-pt page; the editor
  reports its widget rect `[170, 169.8898, 420, 189.8898]`: 841.8898 − 672 = 169.8898.
- A field the agent made at `[170, 250, 420, 270]` was saved as `/Rect [170.0, 571.89, 420.0, 591.89]`: 841.89 − 270, 841.89 − 250.
- The redact mark at `[195.67, 117.75, 231.40, 133.72]` was saved as `/Rect [195.7, 708.2, 231.4, 724.1]`.
- The first recognized shape sits at `x 914400 EMU` = 72 pt, where reportlab started every line.

---

### Q8. All or nothing

**Answer: `StartAction ["GroupActions"]` cannot be used in the PDF editor on 9.3.5: it throws inside the editor, never
answers, and wedges the page's plugin queue for good** (every later command and method times out). Fallback, tested with a
person's editor on the same key: **one batch = one command; on failure, call the `Undo` method immediately** (no other
command in between). It reverts the whole command for both editors and keeps the person's interleaved edit. Unlike a
group, the person **sees the partial state** until the Undo arrives (~2 s here). A committed command reaches the person's
editor in well under a second.

The wedge (`pdf/ga.out`, frame `window.g_asc_plugins.queueCommands`, then reproduced directly in the frame):
```
method StartAction ["GroupActions"] -> {"value":{"__timeout":true}} after 30 s; frame: editor.groupActionsCounter == 1
queueCommands: {"n":2,"head":{"type":1,"name":""}}   (the StartAction method stays at the head forever; EndAction never runs)
editor.startGroupActions() in the frame -> TypeError: logicDocument.GetSpellCheckManager is not a function
    at asc_docs_api._onStartGroupActions (sdk-all-min.js) at baseEditorsApi.startGroupActions
```
`CPluginsManager.callMethodInternal` calls `this.api["pluginMethod_" + name]` without a try/catch, so the throw skips
`shiftCommand` and the queue never moves. **runtime-office's `Office.inGroup` must not be used for PDF.** (Also a host-page
bug seen here: on a timeout the entry stays in `conn.queue`, so a late reply would resolve the wrong request; moot once the
page is closed on timeout, which the runtime does.)

Undo fallback (agent `f2857cbe….agent_pdf`, person `….user_spike` "Flavius", same key):
```js
// agent: two changes, then a failure the command reports
function () { try { var doc = Api.GetDocument(); doc.GetFieldByName("client").SetValue("PARTIAL-1"); doc.GetPage(0).SetRotation(180);
  throw new Error("boom after two changes"); } catch (e) { return { __error: String(e && e.message || e) }; } }
```
```
agent cmd                -> {"__error":"boom after two changes"}
person reads             -> {"client":"PARTIAL-1","signed":"Off","rot0":180}     (the partial batch reached the person)
person ticks "signed"    -> true
agent: method Undo       -> (no value)
agent reads after 1.5 s  -> {"client":"Agent wrote this","signed":"Yes","rot0":0}
person reads             -> {"client":"Agent wrote this","signed":"Yes","rot0":0}  (both changes gone, the person's tick kept)
```
Committed edits seen by the person (agent: `client` value + page 2 rotated + a committed free text; then recognize page 1
and replace "thirty" with "forty-five"); person's `pdf/state.js` ~300 ms after the agent's flush:
```
{"client":"Agent wrote this","pages":2,"rot":[0,90],"annots":[["freeTextAnnot","Agent",null]],"drawings":0,"recognized":false}
{… "drawings":3,"recognized":true,"shapeText":["Service agreement\r\n",
   "The supplier delivers the service from 1 March 2026. Fees are due within forty-five days of each invoice.\r\n","Client name: Signed:\r\n"]}
```
Screenshot of the person's editor (`saved/P-person.png`): "Fees are due within forty-five days of each invoice." and the
field "Agent wrote this", in the person's default "Commenting" mode.

**Gotcha (Q8), important: in the PDF editor every command is an undo point, reads included.**
```
SetValue("STEP-B"); wait; method Undo                 -> client "STEP-A"   (Undo took the edit back)
SetValue("STEP-C"); a read command (GetValue); Undo   -> client "STEP-C"   (Undo took the read's empty point instead)
failing command that changed nothing; Undo            -> previous edit kept (Undo took the failed command's own point)
```
So the rollback Undo is safe exactly when it is the next thing sent after the failed command (the runtime serializes
commands per page; keep it that way, and never retry-with-read in between). A refusal decided *before* any change should
not call Undo at all (it would only eat an empty point, harmless but pointless).

---

### Q9. Flush and forcesave

**Answer:** the host page's `onDocumentStateChange` `false` after the command's `sentAt` is the reliable signal, as for
Word, with two PDF differences: **every command (reads too) fires true then false**, and **alone in the document the
false comes ~0.8–1 s after the command** (with a co-editor connected: 0–40 ms). The flag is also readable from a command:
`Api.GetDocument().Document.Api.isDocumentModified()` (internal; returns false inside the command when everything earlier
was sent, the command's own point not counted). Forcesave via the Command Service works as for Word: `{"error":0}`, status-6
callback in ~3 s with `userdata` echoed and `forcesavetype 0`, the file has the edit.

Evidence:
```
alone (page e211…):    recognize sent …512715 -> true +564, true +723, false +1322 ms; edit sent …521939 -> true +77, false +1052 ms
alone, read only:      GetPagesCount() at …519832 -> true …519833, false …520877
with the person open:  edit command …385601 -> true and false both at …385894; recognize+edit -> true/false at …396092
modified from a command: {"hasApi":true,"modified":false}; events after it: true, true, true, false (+36 ms)
forcesave (pdf/forcesave.py), co-edited key: sent right after the command (no wait) -> {"error":0}, status 6
  userdata "too-early", file has the edit (the change had already gone out); a second forcesave after the flush ->
  {"error":4} (nothing new). Flush wait 268 ms, forcesave -> file on disk 2979 ms.
status-6 callback body (fields): {"key":…,"status":6,"users":["agent:pdf"],"userdata":"too-early","forcesavetype":0,"filetype":"pdf",…}
```
Runtime notes: `Office.flushed` first runs a `modified` command: in PDF that read itself fires a state change, harmless
(it returns false when nothing is pending). Budget ~1 s per flush when the agent is alone, ~3 s per forcesave on DEV.

---

### Open questions

- **Which glyphs an embedded subset has** is not exposed by the builder API (the run's font is `"Embedded: <subset name>
  <hash>"`). Knowing it would let `replace` warn before mixing in Arial. Not found in the time; candidates are the
  editor's embedded-font map (`Asc.editor.embeddedFontsMap`, internal) — not tried.
- **Overlap after growing a recognized box**: `checkExtentsByDocContent(true)` grows it downward over whatever is below;
  I did not find an API that reports the next block's position other than reading all shapes' rects (doable: shapes have
  `GetPosY/GetHeight`).
- **Free text without the internal commit** saves an empty box; is there a public way? `ApiFreeTextAnnotation` has no text
  setter; `SetContents` alone does not fill `/RC`. Only `impl.SetRichContents(impl.GetRichContents(true))` worked.
- **DEV open flakiness:** from ~20 minutes into the run, most PDF opens failed with `errorCode -4 "Download failed."` while
  `.xlsx` opens worked and a standalone Playwright open (`pdf/diag-open.mjs`, fresh browser) of the same file succeeded.
  Retrying (up to 4×) or another user id got through. Not diagnosed; the document server fetched the file each time. If it
  happens in the runtime, its open path needs a retry.
- **"Edit PDF" mode for the person:** the agent's builder edits work in the default "Commenting" mode; I did not test
  what the person's editor does if the person switches to "Edit PDF" mode while the agent edits (that mode's menu text is
  "Add or edit text, shapes, images etc.", string `textEditDescNoCoedit`; the other variant reads "All changes will be
  saved to the file. Real time collaboration").
- The tight-subset file came back a point smaller per run after recognition; whether this is rounding of a scaled text
  matrix (Chromium writes 16 px × 0.75) or something else was not settled.
