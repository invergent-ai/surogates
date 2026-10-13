// The script runtime-office runs in a Word editor through its connector, with `scope` set to the request.
// It runs in the editor's command sandbox (strict mode, no window or document). Every exception is caught
// here, because the editor swallows them; one run is one undo step.
(function () {
  "use strict";
  var doc = Api.GetDocument();
  var MUTATED = false;

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

  // An id the editor still knows can be a block someone deleted: a block is in the document only at its position.
  function attached(el) {
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
  var OUTLINE_BUDGET = 30000, VIEW_BUDGET = 30000;

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
    return parts.join("\n");
  }

  function view(id) {
    var el = Api.GetByInternalId(String(id));
    if (!el || !attached(el)) return { error: "No block " + id + ": read the outline again." };
    var block = describe(el, 4000);
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

  try {
    if (scope.action === "outline") return outline(scope.start || 0, scope.limit || 200);
    if (scope.action === "view") return view(scope.target);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "modified") return doc.Document.Api.isDocumentModified();
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    return { __error: String((e && e.message) || e), __mutated: MUTATED };
  }
})
