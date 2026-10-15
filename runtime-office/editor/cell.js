// The script runtime-office runs in a spreadsheet editor through its connector, with `scope` set to the request.
// It runs in the editor's command sandbox (strict mode, no window or document; `Asc` is the connector's own
// variable, so constants come from AscCommon and AscCommonExcel). Every exception is caught here, because the editor
// swallows them. runtime-office runs an edit inside a group of actions, which it cancels when the edit is refused or
// fails: nothing of a batch is kept unless all of it applied. Internal names are used only where the builder has no
// call, each marked where it is used: the used range's bounds, merged areas, protection, locks, chart ranges.
(function () {
  "use strict";

  var VIEW_BUDGET = 30000, TOP_COLUMNS = 30, MAX_MATCHES = 50, EMU_PER_PX = 9525;
  // The builder's chart types: "bar" draws columns, and "column" and "line" throw.
  var CHARTS = { column: "bar", bar: "horizontalBar", line: "lineNormal", pie: "pie", area: "area", doughnut: "doughnut", scatter: "scatter" };
  var CHART_NAMES = { bar: "column", horizontalBar: "bar", lineNormal: "line" };

  function letters(c) { var s = ""; for (c++; c > 0; c = Math.floor((c - 1) / 26)) s = String.fromCharCode(65 + (c - 1) % 26) + s; return s; }
  function a1(r, c) { return letters(c) + (r + 1); }
  function box(b) { return b.r1 === b.r2 && b.c1 === b.c2 ? a1(b.r1, b.c1) : a1(b.r1, b.c1) + ":" + a1(b.r2, b.c2); }
  function grid(v) { return Array.isArray(v) ? (Array.isArray(v[0]) ? v : [v]) : [[v]]; }
  function quoted(name) { return /^[A-Za-z0-9_.]+$/.test(name) ? name : "'" + name.replace(/'/g, "''") + "'"; }
  function at(sheet, range) { return quoted(sheet) + "!" + range; }
  function absolute(sheet, range) {
    return "'" + sheet.replace(/'/g, "''") + "'!" + range.split(":").map(function (c) { return c.replace(/^([A-Z]+)(\d+)$/, "$$$1$$$2"); }).join(":");
  }
  // The editor behind the builder (internal: the builder has no call for protection, locks or who is connected).
  function editor() { return Api.GetSheets()[0].worksheet.workbook.oApi; }
  function rgb(hex) { var n = parseInt(hex.slice(1), 16); return Api.CreateColorFromRGB((n >> 16) & 255, (n >> 8) & 255, n & 255); }
  function clip(text, max) { return text.length > max ? text.slice(0, max) + " …" : text; }

  function Refusal(op, reason, now) { this.op = op; this.reason = reason; this.now = now; }

  function sheetOf(name, op) {
    var sheet = Api.GetSheet(name);
    if (!sheet) throw new Refusal(op, "There is no sheet " + name + ": read the outline again.");
    return sheet;
  }

  function charts() {
    var out = [];
    Api.GetSheets().forEach(function (s) { s.GetAllCharts().forEach(function (c) { out.push(c); }); });
    return out;
  }

  function chartNamed(name) {
    var key = name.toLowerCase();
    return charts().some(function (c) { return String(c.GetName() || "").toLowerCase() === key; });
  }

  function freeName(base) {
    if (!chartNamed(base)) return base;
    for (var n = 2; ; n++) if (!chartNamed(base + " " + n)) return base + " " + n;
  }

  // Who holds a connection, by the participants the editor knows (internal; the public getUsers() is empty).
  function nameOf(connection) {
    var parts = editor().CoAuthoringApi._CoAuthoringApi._participants || {};
    for (var key in parts) {
      var p = parts[key];
      if (!p) continue;
      var id = typeof p.asc_getId === "function" ? p.asc_getId() : (p.conn || key);
      if (id === connection) return typeof p.asc_getUserName === "function" ? p.asc_getUserName() : (p.name || "Someone");
    }
    return "Someone";
  }

  // --- reading

  // Internal: the builder has no call for a range's bounds (`.range.bbox`), a sheet's protection or merged areas
  // (`worksheet`, `mergeManager`), or a chart's data range and anchor (`Chart.getCommonRange()`, `drawingBase`).
  function outline() {
    var sheets = Api.GetSheets(), out = { workbook_protected: !!editor().asc_isProtectedWorkbook(), sheets: [] };
    for (var i = 0; i < sheets.length; i++) {
      var s = sheets[i], ws = s.worksheet, b = s.GetUsedRange().range.bbox;
      var top = s.GetRange(a1(b.r1, b.c1) + ":" + a1(Math.min(b.r1 + 1, b.r2), Math.min(b.c2, b.c1 + TOP_COLUMNS - 1))).GetText();
      out.sheets.push({
        name: s.GetName(), visible: s.GetVisible(), used: box(b), protected: !!ws.getSheetProtection(), top: grid(top),
        merged: ws.mergeManager.getAll().slice(0, 50).map(function (m) { return box(m.bbox); }),
        charts: s.GetAllCharts().map(function (c) {
          var from = c.Chart.drawingBase && c.Chart.drawingBase.from, type = c.GetChartType();
          return { name: c.GetName(), type: CHART_NAMES[type] || type, title: String(c.GetTitle() || "").replace(/\s+$/, ""),
            data: c.Chart.getCommonRange(), anchor: from ? a1(from.row, from.col) : null };
        }),
      });
    }
    return out;
  }

  // The range as shown (what the person sees, and what "expect" holds), with formulas and number formats beside it,
  // until the budget: the rest is "next".
  function view(name, range) {
    var sheet = Api.GetSheet(name);
    if (!sheet) return { error: "There is no sheet " + name + ": read the outline again." };
    // Internal: the builder has no call for a range's bounds.
    var r = sheet.GetRange(range), b = r.range.bbox, texts = grid(r.GetText()), rows = [], formulas = {}, formats = {}, size = 0;
    for (var i = 0; i < texts.length; i++) {
      if (size > VIEW_BUDGET) {
        return { range: at(name, box({ r1: b.r1, c1: b.c1, r2: b.r1 + i - 1, c2: b.c2 })), values: rows, formulas: formulas, formats: formats,
          next: at(name, box({ r1: b.r1 + i, c1: b.c1, r2: b.r2, c2: b.c2 })) };
      }
      for (var j = 0; j < texts[i].length; j++) {
        var c = sheet.GetRangeByNumber(b.r1 + i, b.c1 + j), f = c.GetFormula(), fmt = c.GetNumberFormat(), ref = a1(b.r1 + i, b.c1 + j);
        // A formula reads "= B6+C6": the space after "=" is the editor's.
        if (typeof f === "string" && f.charAt(0) === "=") { formulas[ref] = "=" + f.slice(1).replace(/^ /, ""); size += ref.length + f.length + 6; }
        if (fmt && fmt !== "General") { formats[ref] = fmt; size += ref.length + fmt.length + 6; }
      }
      rows.push(texts[i]);
      size += JSON.stringify(texts[i]).length;
    }
    return { range: at(name, box(b)), values: rows, formulas: formulas, formats: formats };
  }

  // A scan of each sheet's shown text: the builder's Find misses matches and finds false ones. The used range's
  // bounds are internal (`.range.bbox`): the builder has no call for them.
  function search(query) {
    var q = String(query).toLowerCase(), sheets = Api.GetSheets(), matches = [], total = 0;
    for (var s = 0; s < sheets.length; s++) {
      var used = sheets[s].GetUsedRange(), b = used.range.bbox, texts = grid(used.GetText());
      for (var i = 0; i < texts.length; i++) {
        for (var j = 0; j < texts[i].length; j++) {
          var text = String(texts[i][j]);
          if (text.toLowerCase().indexOf(q) < 0) continue;
          total++;
          if (matches.length < MAX_MATCHES) matches.push({ cell: at(sheets[s].GetName(), a1(b.r1 + i, b.c1 + j)), text: clip(text, 200) });
        }
      }
    }
    return { matches: matches, total: total };
  }

  // --- sheets: checked whole, then changed; adds and deletes land after the command when a co-editor is connected

  function sheets(entries) {
    // Internal: the builder has no call for the workbook's protection, and its AddSheet and SetName ignore it.
    if (editor().asc_isProtectedWorkbook()) {
      throw new Refusal(entries[0].index, "The workbook's structure is protected: no sheet may be added, renamed or deleted. Only the user can unprotect it.");
    }
    var all = Api.GetSheets(), names = all.map(function (s) { return s.GetName().toLowerCase(); });
    var visible = all.filter(function (s) { return s.GetVisible(); }).length;
    entries.forEach(function (entry) {
      var op = entry.op, key = op.name.toLowerCase();
      if (op.action === "add") {
        if (names.indexOf(key) >= 0) throw new Refusal(entry.index, "A sheet named " + op.name + " exists.");
        if (op.after) sheetOf(op.after, entry.index);
        names.push(key);
        visible++;
        return;
      }
      var sheet = sheetOf(op.name, entry.index);
      if (op.action === "rename") {
        if (op.to.toLowerCase() !== key && names.indexOf(op.to.toLowerCase()) >= 0) throw new Refusal(entry.index, "A sheet named " + op.to + " exists.");
        return;
      }
      if (sheet.GetVisible() && --visible < 1) throw new Refusal(entry.index, "A workbook keeps one visible sheet: " + op.name + " is its last.");
    });
    var pending = [], results = {};
    entries.forEach(function (entry) {
      var op = entry.op;
      if (op.action === "add") {
        // A new sheet goes after the agent's active sheet (its own, not the person's).
        var list = Api.GetSheets();
        (op.after ? Api.GetSheet(op.after) : list[list.length - 1]).SetActive();
        Api.AddSheet(op.name);
        pending.push({ add: op.name });
        results[entry.index] = { op: entry.index, sheet: op.name };
      } else if (op.action === "rename") {
        Api.GetSheet(op.name).SetName(op.to);
        results[entry.index] = { op: entry.index, sheet: op.to };
      } else {
        Api.GetSheet(op.name).Delete();
        pending.push({ gone: op.name });
        results[entry.index] = { op: entry.index, sheet: op.name };
      }
    });
    return { pending: pending, results: results };
  }

  function landed(pending) {
    for (var i = 0; i < pending.length; i++) {
      if (pending[i].add && !Api.GetSheet(pending[i].add)) return false;
      if (pending[i].gone && Api.GetSheet(pending[i].gone)) return false;
    }
    return true;
  }

  // --- locks: builder writes take none, so a cell two editors write would end up different in each. The agent
  // asks the editor's own range locks, as typing in a cell does; the server answers after the command. All of it is
  // internal (`collaborativeEditing`, `getLockInfo`, `_isLockedCells`): no builder call takes or reports a lock.

  function lock(ranges, ticket) {
    var api = editor(), ce = api.collaborativeEditing, bySheet = {}, order = [];
    for (var i = 0; i < ranges.length; i++) {
      var sheet = sheetOf(ranges[i].sheet, ranges[i].index), ws = sheet.worksheet, b = sheet.GetRange(ranges[i].range).range.bbox;
      var info = ce.getLockInfo(AscCommonExcel.c_oAscLockTypeElem.Range, null, ws.getId(), new AscCommonExcel.asc_CCollaborativeRange(b.c1, b.r1, b.c2, b.r2));
      var other = ce.getLockIntersection(info, AscCommon.c_oAscLockTypes.kLockTypeOther, false);
      if (other) {
        return { state: "refused", op: ranges[i].index, reason: nameOf(other.UserId) + " is editing " + at(ranges[i].sheet, ranges[i].range) + " right now: nothing applied. Try again in a moment." };
      }
      if (!bySheet[ws.getId()]) { bySheet[ws.getId()] = { name: sheet.GetName(), index: ranges[i].index, ws: ws, boxes: [] }; order.push(ws.getId()); }
      bySheet[ws.getId()].boxes.push(b);
    }
    // The answers arrive after this command: they are kept on `Api`, the same object in every command, under a
    // name of the agent's own (`__agentLocks`, no builder name), for `locks` to read back.
    var store = Api.__agentLocks || (Api.__agentLocks = {}), answer = store[ticket] = { left: order.length, refused: null };
    order.forEach(function (id) {
      var entry = bySheet[id];
      api.wb.getWorksheet(entry.ws.getIndex())._isLockedCells(entry.boxes, null, function (ok) {
        answer.left--;
        if (!ok && !answer.refused) answer.refused = entry;
      });
    });
    return locks(ticket);
  }

  function locks(ticket) {
    var store = Api.__agentLocks || {}, answer = store[ticket];
    if (!answer) return { state: "refused", op: 0, reason: "The editor lost the request for the cells: nothing applied. Try again." };
    if (answer.refused) {
      delete store[ticket];
      return { state: "refused", op: answer.refused.index, reason: "Someone is editing " + answer.refused.name + " right now: nothing applied. Try again in a moment." };
    }
    if (answer.left > 0) return { state: "pending" };
    delete store[ticket];
    return { state: "granted" };
  }

  // --- writes: every check first, against the workbook as it is before the batch (a formula an earlier op wrote is
  // not computed until the command returns)

  function differs(range, expect) {
    var now = grid(range.GetText());
    if (now.length !== expect.length) return now;
    for (var i = 0; i < now.length; i++) {
      if (now[i].length !== expect[i].length) return now;
      for (var j = 0; j < now[i].length; j++) if (String(now[i][j]) !== expect[i][j]) return now;
    }
    return null;
  }

  // Internal: the builder has no call for a sheet's protection (`sheetProtection`, `getSheetProtection`) or its
  // locked cells (`isIntersectLockedRanges`, on a range's internal bounds).
  function check(op, index) {
    var sheet = sheetOf(op.op === "chart" ? op.on : op.sheet, index), ws = sheet.worksheet, p = ws.sheetProtection, guarded = !!ws.getSheetProtection();
    if (op.op === "set") {
      var range = sheet.GetRange(op.range);
      // The builder refuses a locked cell silently: say it.
      if (guarded && ws.isIntersectLockedRanges([range.range.bbox])) {
        throw new Refusal(index, at(op.sheet, op.range) + " is in a protected part of " + op.sheet + ": only the user can unprotect it.");
      }
      if (op.expect) {
        var now = differs(range, op.expect);
        if (now) throw new Refusal(index, at(op.sheet, op.range) + " changed since you read it. This is what it shows now; work from it.", now);
      }
    } else if (op.op === "format") {
      // A column width ignores protection in the builder: refused here with the rest.
      if (guarded && (p.asc_getFormatCells() || (op.style.width !== undefined && p.asc_getFormatColumns()))) {
        throw new Refusal(index, op.sheet + " is protected against formatting: only the user can unprotect it.");
      }
    } else if (op.op === "chart") {
      sheetOf(op.sheet, index);
      if (guarded && p.asc_getObjects()) throw new Refusal(index, op.on + " is protected against charts: only the user can unprotect it.");
      if (op.name && chartNamed(op.name)) throw new Refusal(index, "A chart named " + op.name + " exists: name this one differently.");
    }
  }

  var APPLY = {
    set: function (op, index) {
      Api.GetSheet(op.sheet).GetRange(op.range).SetValue(op.values);
      return { op: index, range: at(op.sheet, op.range) };
    },
    format: function (op, index) {
      var r = Api.GetSheet(op.sheet).GetRange(op.range), s = op.style;
      if (s.number_format !== undefined) r.SetNumberFormat(s.number_format);
      if (s.bold !== undefined) r.SetBold(s.bold);
      if (s.italic !== undefined) r.SetItalic(s.italic);
      if (s.wrap !== undefined) r.SetWrap(s.wrap);
      // Api.HexColor makes a colour the spreadsheet setters cannot read, and the fill is dropped.
      if (s.fill !== undefined) r.SetFillColor(s.fill === "none" ? "No Fill" : rgb(s.fill));
      if (s.color !== undefined) r.SetFontColor(rgb(s.color));
      if (s.align !== undefined) r.SetAlignHorizontal(s.align);
      if (s.valign !== undefined) r.SetAlignVertical(s.valign);
      if (s.width !== undefined) r.SetColumnWidth(s.width);
      return { op: index, range: at(op.sheet, op.range) };
    },
    chart: function (op, index) {
      // A chart has no public id: the name the agent gives it is its handle, and it survives in the file.
      var name = op.name || freeName(op.title || "Chart");
      var chart = Api.GetSheet(op.on).AddChart(absolute(op.sheet, op.range), false, CHARTS[op.type], 2,
        op.width * EMU_PER_PX, op.height * EMU_PER_PX, op.anchor.col, 0, op.anchor.row, 0);
      if (!chart) throw new Error("the editor made no chart");
      if (op.title) chart.SetTitle(op.title, 12);
      chart.SetName(name);
      return { op: index, chart: name };
    },
  };

  function edit(entries) {
    entries.forEach(function (entry) { check(entry.op, entry.index); });
    var results = {};
    entries.forEach(function (entry) { results[entry.index] = APPLY[entry.op.op](entry.op, entry.index); });
    return { results: results };
  }

  try {
    if (scope.action === "outline") return outline();
    if (scope.action === "range") return view(scope.sheet, scope.range);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "sheets") return sheets(scope.ops);
    if (scope.action === "landed") return landed(scope.pending);
    if (scope.action === "lock") return lock(scope.ranges, scope.ticket);
    if (scope.action === "locks") return locks(scope.ticket);
    if (scope.action === "edit") return edit(scope.ops);
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    if (e instanceof Refusal) return { refused: { op: e.op, reason: e.reason, now: e.now } };
    return { __error: String((e && e.message) || e) };
  }
})
