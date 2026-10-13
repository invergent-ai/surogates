// The script runtime-office runs in a Word editor through its connector, with `scope` set to the request.
// It runs in the editor's command sandbox (strict mode, no window or document). Every exception is caught
// here, because the editor swallows them. runtime-office runs an edit inside a group of actions, which it cancels
// when the edit is refused or fails: nothing of a batch is kept unless all of it applied.
(function () {
  "use strict";
  var doc = Api.GetDocument();

  function hash(text) {
    var h = 0x811c9dc5;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
  }

  function textOf(el) {
    var raw = el.GetClassType() === "paragraph" ? el.GetText({ Numbering: false }) : el.GetRange().GetText();
    return String(raw || "").replace(/\r\n/g, "\n").replace(/\s+$/, "");
  }

  function words(text, count) {
    var all = text.split(/\s+/).filter(Boolean);
    return all.length > count ? all.slice(0, count).join(" ") + " …" : all.join(" ");
  }

  function clip(text, max) {
    return text.length > max ? text.slice(0, max) + " …" : text;
  }

  // An id the editor still knows can be a block someone deleted: a block is in the document only at its position.
  // Rows, cells, drawings and inline controls have ids too, but no position: they are not blocks.
  function attached(el) {
    if (typeof el.GetPosInParent !== "function") return false;
    var pos = el.GetPosInParent(), top = pos >= 0 ? doc.GetElement(pos) : null;
    return !!top && String(top.GetInternalId()) === String(el.GetInternalId());
  }

  // Matches in document order: the editor returns them by paragraph id, and new paragraphs' ids sort last.
  function find(text, matchCase) {
    return doc.Search(String(text), matchCase).sort(function (a, b) { return a.GetStartPos() - b.GetStartPos(); });
  }

  function kindOf(el) {
    var type = el.GetClassType();
    if (type === "table") return "table";
    if (type === "blockLvlSdt") return /^pending:/.test(el.GetTag() || "") ? "pending" : "contentControl";
    if (type !== "paragraph") return type;
    var style = el.GetStyle(), name = style ? style.GetName() : "";
    var heading = /^heading (\d)$/i.exec(name);
    if (heading) return "heading" + heading[1];
    // A numbered item's text starts with its number; a bullet's symbol is not in the text.
    if (el.GetNumbering()) return /^\s*\d/.test(el.GetText({ Numbering: true })) ? "numbered" : "bullet";
    if (el.GetAllDrawingObjects().length && !textOf(el)) return "image";
    return "paragraph";
  }

  function describe(el, wordCount) {
    var text = textOf(el), kind = kindOf(el);
    var block = { id: String(el.GetInternalId()), hash: hash(text), kind: kind };
    if (kind === "pending") {
      block.key = el.GetTag().slice("pending:".length);
      block.intent = el.GetPlaceholderText();
    } else {
      block.text = words(text, wordCount);
    }
    if (kind === "table") block.rows = el.GetRowsCount();
    return block;
  }

  // Each top-level block with its span, to find the block a match is in.
  function spans() {
    var out = [];
    for (var i = 0, n = doc.GetElementsCount(); i < n; i++) {
      var el = doc.GetElement(i), range = el.GetRange();
      out.push({ el: el, start: range.GetStartPos(), end: range.GetEndPos() });
    }
    return out;
  }

  function blockAt(index, range) {
    var pos = range.GetStartPos();
    for (var i = 0; i < index.length; i++) if (pos >= index[i].start && pos <= index[i].end) return index[i].el;
    return null;
  }

  function comments() {
    return doc.GetAllComments().slice(0, 30).map(function (c) {
      return {
        id: String(c.GetId()), author: c.GetAuthorName(), quote: words(c.GetQuoteText() || "", 8),
        text: words(c.GetText() || "", 30), replies: c.GetRepliesCount(), resolved: !!c.IsSolved(),
      };
    });
  }

  function trackedChanges() {
    var report = doc.GetReviewReport(), out = {};
    // The report carries a ToJSON function among the authors.
    for (var name in report) if (Array.isArray(report[name])) out[name] = report[name].length;
    return out;
  }

  // A page of the outline, or one table, stays well under the 50,000 characters a tool result may hold.
  var OUTLINE_BUDGET = 30000, VIEW_BUDGET = 30000, CELL_BUDGET = 2000;

  function outline(start, limit) {
    var n = doc.GetElementsCount(), blocks = [], size = 0;
    for (var i = start; i < n && blocks.length < limit && size < OUTLINE_BUDGET; i++) {
      var block = describe(doc.GetElement(i), 14);
      size += JSON.stringify(block).length;
      blocks.push(block);
    }
    var result = { blocks: blocks, total: n };
    if (start + blocks.length < n) result.next = start + blocks.length;
    if (start === 0) {
      result.comments = comments();
      result.tracked_changes = trackedChanges();
    }
    return result;
  }

  function cellText(cell) {
    var content = cell.GetContent(), parts = [];
    for (var i = 0; i < content.GetElementsCount(); i++) {
      var el = content.GetElement(i);
      if (el.GetClassType() === "paragraph") parts.push(String(el.GetText({ Numbering: false })).replace(/\s+$/, ""));
    }
    return clip(parts.join("\n"), CELL_BUDGET);
  }

  function view(id) {
    var el = Api.GetByInternalId(String(id));
    if (!el || !attached(el)) return { error: "No block " + id + ": read the outline again." };
    var block = describe(el, 0);
    // The exact text, whitespace kept: a quote copied from it must find the same words again.
    if (block.text !== undefined) block.text = clip(textOf(el), VIEW_BUDGET);
    if (block.kind === "table") {
      block.cells = [];
      var size = 0;
      for (var r = 0; r < el.GetRowsCount(); r++) {
        if (size > VIEW_BUDGET) {
          block.truncated_at_row = r;
          break;
        }
        var row = el.GetRow(r), cells = [];
        for (var c = 0; c < row.GetCellsCount(); c++) cells.push(cellText(row.GetCell(c)));
        size += JSON.stringify(cells).length;
        block.cells.push(cells);
      }
      delete block.text;
    }
    return block;
  }

  function search(query) {
    var found = find(query, false), index = spans(), matches = [];
    for (var i = 0; i < found.length && matches.length < 50; i++) {
      var el = blockAt(index, found[i]);
      if (!el) continue;
      var block = describe(el, 24);
      block.match = found[i].GetText();
      matches.push(block);
    }
    return { matches: matches, total: found.length };
  }

  function Refusal(reason, el) {
    this.reason = reason;
    this.block = el ? describe(el, 80) : null;
  }

  function resolveTarget(target) {
    if (target.end) return { end: true };
    if (target.block !== undefined) {
      var el = Api.GetByInternalId(String(target.block));
      if (!el || !attached(el)) {
        throw new Refusal("Block " + target.block + " is gone: deleted or moved since you read it, or by an earlier op of this call. Read the outline again.");
      }
      if (hash(textOf(el)) !== target.hash) {
        throw new Refusal("Block " + target.block + " changed since you read it. This is it now; work from it.", el);
      }
      return { el: el };
    }
    if (target.pending !== undefined) {
      var sdt = doc.GetContentControlsByTag("pending:" + target.pending)[0];
      if (!sdt) throw new Refusal("The pending block " + target.pending + " is gone: the user deleted it (leave that section out), or an earlier op of this call filled or deleted it.");
      return { el: sdt, pending: true };
    }
    var found = find(target.find, true);
    if (found.length < target.nth) {
      throw new Refusal(found.length
        ? "Only " + found.length + " match(es) of \"" + target.find + "\"."
        : "\"" + target.find + "\" is not in the document now: it changed since you read it, or an earlier op of this call changed it. Read it again.");
    }
    var range = found[target.nth - 1];
    return { el: blockAt(spans(), range), range: range };
  }

  // A new document holds one empty paragraph: what is written first at its end takes that line's place.
  function onlyBlankLine() {
    if (doc.GetElementsCount() !== 1) return null;
    var el = doc.GetElement(0);
    return el.GetClassType() === "paragraph" && !textOf(el) && !el.GetAllDrawingObjects().length ? el : null;
  }

  function position(where, side) {
    if (where.end) return doc.GetElementsCount();
    var pos = where.el.GetPosInParent();
    return side === "before" ? pos : pos + 1;
  }

  function addRuns(paragraph, runs, size) {
    for (var i = 0; i < runs.length; i++) {
      var run = runs[i];
      if (run.href) {
        try {
          paragraph.AddElement(Api.CreateHyperlink(run.href, run.text, ""));
          continue;
        } catch (e) {
          // The text stays, without its link.
        }
      }
      var lines = String(run.text).split("\n");
      for (var k = 0; k < lines.length; k++) {
        if (k > 0) paragraph.AddLineBreak();
        if (!lines[k]) continue;
        var added = paragraph.AddText(lines[k]);
        if (run.b || size) added.SetBold(true);
        if (run.i) added.SetItalic(true);
        if (run.s) added.SetStrikeout(true);
        if (run.code) added.SetFontFamily("Courier New");
        if (size) added.SetFontSize(size);
      }
    }
  }

  function paragraphOf(runs, styleName, level) {
    var paragraph = Api.CreateParagraph(), style = styleName ? doc.GetStyle(styleName) : null;
    if (style) paragraph.SetStyle(style);
    // A document without heading styles gets bold, larger text instead (sizes in half-points).
    addRuns(paragraph, runs, styleName && !style && level ? [0, 32, 28, 24][level] : 0);
    return paragraph;
  }

  function bold(runs) {
    return runs.map(function (run) {
      var copy = {};
      for (var key in run) copy[key] = run[key];
      copy.b = true;
      return copy;
    });
  }

  function tableOf(rows) {
    var columns = 1;
    for (var r = 0; r < rows.length; r++) columns = Math.max(columns, rows[r].length);
    var table = Api.CreateTable(columns, rows.length), grid = doc.GetStyle("Table Grid");
    if (grid) table.SetStyle(grid);
    for (var row = 0; row < rows.length; row++) {
      for (var c = 0; c < rows[row].length; c++) {
        addRuns(table.GetCell(row, c).GetContent().GetElement(0), row === 0 ? bold(rows[row][c]) : rows[row][c], 0);
      }
    }
    return table;
  }

  function pendingOf(key, intent) {
    var sdt = Api.CreateBlockLvlSdt();
    sdt.SetTag("pending:" + key);
    sdt.SetAlias("The agent is writing this");
    sdt.SetPlaceholderText(intent);
    // The user cannot type into it, but one Delete removes it.
    sdt.SetLock("contentLocked");
    sdt.SetBackgroundColor(230, 240, 255);
    sdt.SetBorderColor(80, 120, 200);
    return sdt;
  }

  // Pixels at 96 dpi as EMUs; nothing wider than the six inches a page's text usually spans, nor higher than nine.
  var EMU_PER_PX = 9525, MAX_WIDTH_PX = 576, MAX_HEIGHT_PX = 864;
  var CHART_TYPES = {
    bar: "bar", column: "bar", "horizontal-bar": "horizontalBar", line: "lineNormal",
    pie: "pie", area: "area", doughnut: "doughnut", scatter: "scatter",
  };

  function imageOf(b) {
    var scale = Math.min(1, MAX_WIDTH_PX / b.width, MAX_HEIGHT_PX / b.height), paragraph = Api.CreateParagraph();
    paragraph.AddDrawing(Api.CreateImage(b.src, Math.round(b.width * scale * EMU_PER_PX), Math.round(b.height * scale * EMU_PER_PX)));
    return paragraph;
  }

  function chartOf(b) {
    var paragraph = Api.CreateParagraph();
    var chart = Api.CreateChart(
      CHART_TYPES[b.chart] || "bar",
      b.series.map(function (s) { return s.values; }),
      b.series.map(function (s) { return s.name; }),
      b.categories,
      MAX_WIDTH_PX * EMU_PER_PX,
      336 * EMU_PER_PX,
      24
    );
    if (b.title) chart.SetTitle(b.title, 13);
    paragraph.AddDrawing(chart);
    return paragraph;
  }

  function build(blocks) {
    var out = [], lists = {};
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (b.t === "h") out.push(paragraphOf(b.runs, "Heading " + b.level, b.level));
      else if (b.t === "p") out.push(paragraphOf(b.runs));
      else if (b.t === "li") {
        var numbering = lists[b.list] || (lists[b.list] = doc.CreateNumbering(b.ordered ? "numbered" : "bullet"));
        var item = paragraphOf(b.runs);
        item.SetNumbering(numbering.GetLevel(Math.min(b.level, 8)));
        out.push(item);
      } else if (b.t === "code") {
        for (var l = 0; l < b.lines.length; l++) out.push(paragraphOf([{ text: b.lines[l], code: true }]));
      } else if (b.t === "table") out.push(tableOf(b.rows));
      else if (b.t === "pending") out.push(pendingOf(b.key, b.intent));
      else if (b.t === "image") out.push(imageOf(b));
      else if (b.t === "chart") out.push(chartOf(b));
      else throw new Error("unknown block " + b.t);
    }
    return out;
  }

  function insertAt(pos, elements) {
    var ids = [];
    for (var i = 0; i < elements.length; i++) {
      doc.AddElement(pos + i, elements[i]);
      ids.push(String(elements[i].GetInternalId()));
    }
    return ids;
  }

  function fill(sdt, elements) {
    sdt.SetLock("unlocked");
    sdt.RemoveAllElements();
    for (var i = 0; i < elements.length; i++) sdt.AddElement(elements[i], i);
    var ids = elements.map(function (el) { return String(el.GetInternalId()); });
    sdt.Delete(true);
    return ids;
  }

  function remove(where) {
    if (where.range) where.range.Delete();
    else if (where.pending) where.el.Delete(false);
    else where.el.Delete();
  }

  var EDITS = {
    insert: function (op) {
      var where = resolveTarget(op.target), blank = where.end ? onlyBlankLine() : null;
      var ids = insertAt(position(where, op.side), build(op.blocks));
      if (blank) blank.Delete();
      return { ids: ids };
    },
    replace: function (op) {
      var where = resolveTarget(op.target), elements = build(op.blocks);
      if (where.pending) return { ids: fill(where.el, elements) };
      var ids = insertAt(where.el.GetPosInParent(), elements);
      remove(where);
      return { ids: ids };
    },
    reword: function (op) {
      var where = resolveTarget(op.target), start = where.range.GetStartPos(), length = where.range.GetText().length;
      // The new words go in after the old, inside its last run, so they keep its formatting; then the old go.
      where.range.AddText(op.text, "after");
      var old = doc.GetRange(start, start + length);
      if (old.GetText() !== op.target.find) throw new Error("the words moved while they were being changed");
      old.Delete();
      return {};
    },
    delete: function (op) {
      remove(resolveTarget(op.target));
      return {};
    },
    move: function (op) {
      var what = resolveTarget(op.target), to = resolveTarget(op.to);
      if (to.el && String(to.el.GetInternalId()) === String(what.el.GetInternalId())) throw new Refusal("A block cannot move next to itself.");
      var copy = what.el.Copy();
      insertAt(position(to, op.side), [copy]);
      remove(what);
      return { ids: [String(copy.GetInternalId())] };
    },
    comment: function (op, agent) {
      var where = resolveTarget(op.anchor);
      var comment = where.range.AddComment(op.body, agent.name, agent.id);
      if (!comment) throw new Refusal("The quoted words are inside a locked part of the document; anchor the comment elsewhere.");
      return { thread: String(comment.GetId()) };
    },
    reply: function (op, agent) {
      changeThread(op.thread, function (data) { data.Add_Reply(replyOf(op.body, agent)); });
      return {};
    },
    resolve: function (op) {
      changeThread(op.thread, function (data) { data.SetSolved(true); });
      return {};
    },
  };

  // The Builder's AddReply and SetSolved change a comment's data in place, so cancelling the edit cannot take them
  // back: a copy is changed and set instead, and the history keeps the old data. (Internal, unversioned names.)
  function changeThread(thread, change) {
    var found = doc.GetCommentById(String(thread));
    if (!found) throw new Refusal("Comment thread " + thread + " is gone.");
    var data = found.Comment.GetData().Copy();
    change(data);
    doc.Document.EditComment(found.Comment.GetId(), data);
  }

  // As the Builder's AddReply makes it.
  function replyOf(text, agent) {
    var reply = new AscCommon.CCommentData(), now = new Date();
    reply.SetText(text);
    reply.SetUserName(agent.name);
    reply.m_sUserId = agent.id;
    reply.m_sProviderId = "Teamlab";
    reply.m_sTime = String(now.getTime() - now.getTimezoneOffset() * 60000);
    reply.m_sOOTime = String(now.getTime());
    return reply;
  }

  function edit(ops, agent) {
    var results = [];
    for (var i = 0; i < ops.length; i++) {
      try {
        var result = EDITS[ops[i].op](ops[i], agent);
        result.op = i;
        results.push(result);
      } catch (e) {
        if (e instanceof Refusal) return { refused: { op: i, reason: e.reason, block: e.block } };
        throw e;
      }
    }
    return { results: results };
  }

  try {
    if (scope.action === "edit") return edit(scope.ops, scope.agent);
    if (scope.action === "outline") return outline(scope.start || 0, scope.limit || 200);
    if (scope.action === "view") return view(scope.target);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "modified") return doc.Document.Api.isDocumentModified();
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    return { __error: String((e && e.message) || e) };
  }
})
