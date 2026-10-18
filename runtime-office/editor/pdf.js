// The script runtime-office runs in a PDF editor through its connector, with `scope` set to the request. It runs in
// the editor's command sandbox (strict mode, no window or document). Every exception is caught here, because the
// editor swallows them. The PDF editor cannot start a group of actions: a batch is this one command, which decides
// every refusal before it changes anything, and runtime-office undoes it at once if it fails partway. Coordinates are
// points from the page's top-left, y down, as the editor takes them. Internal names are used where the builder has no
// call; each says so.
(function () {
  "use strict";
  var doc = Api.GetDocument(), TEXT_BUDGET = 30000, MAX_MATCHES = 50, MAX_SEARCH_PAGES = 200, MAX_REPLIES = 10;
  // The editor rounds a new stamp's rect to whole millimetres and, when it next draws it, scales the stamp to that
  // rect: measured, up to about 6 points a side beyond the size it was made at.
  var STAMP_GROWTH = 6;

  function clip(text, max) { return text.length > max ? text.slice(0, max) + " …" : text; }
  // A page's text: the builder has no call for it, so the whole page is selected and the selection read; the
  // selection is the agent's own.
  function pageText(page) {
    page.SetSelection({ x: 0.01, y: 0.01 }, { x: 100000, y: 100000 });
    return String(page.GetSelectedText() || "");
  }
  // Internal: the builder reports no page size; the file's page holds it in points, rounded.
  function sizeOf(index) { var file = doc.Document.GetFile().pages[index]; return [file.W, file.H]; }
  function fieldsOf(page) {
    return page.GetAllWidgets().map(function (w) {
      var f = w.Field;   // internal: the builder has no call from a widget to its field, which holds the name and value
      var checkbox = w.GetClassType() === "checkboxWidget";
      return { name: f.GetFullName(), type: checkbox ? "checkbox" : w.GetClassType().replace(/Widget$/, ""),
        value: checkbox ? w.IsChecked() : f.GetParentValue ? (f.GetParentValue() === undefined ? null : f.GetParentValue()) : null, rect: w.GetRect() };
    });
  }
  // An annotation and its replies make a comment thread. A thread is named by the annotation's id, the same in every
  // co-editor's editor; the id the editor's mentions carry is made anew each time. Internal: the id (`Annot.GetId()`),
  // the user id (`Annot.GetUserId()`) and the state (`Annot.GetState()`), which no builder call reads.
  function solved(a) {
    if (typeof a.Annot.GetState !== "function") return false;   // a stamp or a free text has no state
    var state = a.Annot.GetState();
    return state === 2 || state === 5;   // Accepted, or Completed
  }
  function annotationOf(a) {
    return { id: a.Annot.GetId(), type: a.GetClassType().replace(/Annot$/, ""), rect: a.GetRect(), author: a.GetAuthorName(),
      text: a.GetContents() || "", resolved: solved(a),
      replies: a.GetReplies().slice(-MAX_REPLIES).map(function (r) { return { author: r.GetAuthorName(), text: clip(r.GetContents() || "", 300) }; }) };
  }
  function annotationsOf(page) { return page.GetAllAnnots().map(annotationOf); }

  // --- reading

  // Pages up to the limit and the budget: with its comment threads the outline stays under what a tool result holds. A
  // call lists at least its first page, so the next call starts further on.
  function outline(start, limit) {
    var count = doc.GetPagesCount(), pages = [], size = 0;
    for (var i = start; i < count && pages.length < limit; i++) {
      var page = doc.GetPage(i), text = pageText(page);
      var entry = { page: i + 1, size: sizeOf(i), rotation: page.GetRotation(),
        first_line: clip(text.split("\n").filter(function (l) { return l.trim(); })[0] || "", 120), chars: text.length,
        fields: fieldsOf(page), annotations: annotationsOf(page) };
      size += JSON.stringify(entry).length;
      if (pages.length && size > TEXT_BUDGET) break;
      pages.push(entry);
    }
    var out = { pages: pages, total: count };
    if (start + pages.length < count) out.next = start + pages.length;
    return out;
  }
  function view(number) {
    if (number > doc.GetPagesCount()) return { error: "There is no page " + number + ": the document has " + doc.GetPagesCount() + "." };
    return { page: number, text: clip(pageText(doc.GetPage(number - 1)), TEXT_BUDGET) };
  }
  function search(query) {
    var q = String(query).toLowerCase(), matches = [], total = 0, count = Math.min(doc.GetPagesCount(), MAX_SEARCH_PAGES);
    for (var i = 0; i < count; i++) {
      var text = pageText(doc.GetPage(i)), lower = text.toLowerCase();
      for (var at = lower.indexOf(q); at >= 0; at = lower.indexOf(q, at + q.length)) {
        total++;
        if (matches.length < MAX_MATCHES) matches.push({ page: i + 1, text: clip(text.slice(Math.max(0, at - 60)).replace(/\s+/g, " "), 160) });
      }
    }
    return { matches: matches, total: total };
  }

  // --- comment threads

  // When an annotation or reply was made, comparable: the editor keeps it as milliseconds in text ("1792141582257"),
  // turning a file's PDF dates so as it reads them. One with no date counts as the oldest.
  function madeAt(x) {
    var made = Number(x.GetCreationDate());
    return made > 0 ? made : -1;
  }

  // The thread a mention came from. Its id is no use, being made anew each time, so the thread is found by its words:
  // the newest annotation or reply by that person reading exactly so. A reply's words give its annotation. Editing a
  // comment sends no mention, so the words cannot have changed since.
  function threadOf(query, author) {
    var best = null;
    for (var p = 0; p < doc.GetPagesCount(); p++) {
      doc.GetPage(p).GetAllAnnots().forEach(function (a) {
        [a].concat(a.GetReplies()).forEach(function (x) {
          if (x.GetContents() !== query || x.Annot.GetUserId() !== author) return;
          var made = madeAt(x);
          if (!best || made >= best.made) best = { made: made, annot: a, page: p + 1 };
        });
      });
    }
    if (!best) return { thread: null };
    var thread = annotationOf(best.annot);
    thread.page = best.page;
    return { thread: thread };
  }

  function annotById(id) {
    for (var p = 0; p < doc.GetPagesCount(); p++) {
      var found = doc.GetPage(p).GetAllAnnots().filter(function (a) { return a.Annot.GetId() === id; })[0];
      if (found) return found;
    }
    return null;
  }

  function onlyComments(ops) { return ops.every(function (op) { return op.op === "reply" || op.op === "resolve"; }); }

  function comment(ops, agent) {
    var found = Object.create(null);
    ops.forEach(function (op, index) {
      if (!found[op.thread]) found[op.thread] = annotById(op.thread);
      if (!found[op.thread]) throw new Refusal(index, "Comment thread " + op.thread + " is gone: its comment was deleted, or the page it was on was removed. Read the outline again.");
    });
    // Every reply before any resolve, whatever the ops' order: a resolve sets the comment's data with the history off,
    // so nothing that can fail and be undone may come after it.
    ops.forEach(function (op) {
      if (op.op !== "reply") return;
      var target = found[op.thread], reply = Api.CreateTextAnnot(target.GetRect());
      reply.SetContents(op.body);
      reply.SetAuthorName(agent.name);
      reply.Annot.SetUserId(agent.id);   // internal: the builder sets no user id
      if (!target.AddReply(reply)) throw new Error("the editor took no reply");
    });
    ops.forEach(function (op) {
      if (op.op !== "resolve") return;
      // Internal, the editor's own resolve. It sets the comment's whole data and deletes every reply missing from it,
      // so the data is read from the annotation as it is now, this call's replies included.
      var target = found[op.thread], data = new AscCommon.CCommentData();
      data.Read_FromAscCommentData(target.Annot.GetAscCommentData());
      data.SetSolved(true);
      doc.Document.EditComment(target.Annot.GetId(), data);
    });
    return { results: ops.map(function (op, index) { return { op: index }; }) };
  }

  // --- a note placed by its words

  // A quad's box, [x1, y1, x2, y2]: the editor gives words and lines as their four corners.
  function box(quad) {
    var xs = [quad[0], quad[2], quad[4], quad[6]], ys = [quad[1], quad[3], quad[5], quad[7]];
    return [Math.min.apply(null, xs), Math.min.apply(null, ys), Math.max.apply(null, xs), Math.max.apply(null, ys)];
  }
  function overlaps(a, b) { return a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]; }
  function tenth(n) { return Math.round(n * 10) / 10; }
  function clamp(n, low, high) { return Math.max(low, Math.min(n, high)); }

  // A page's text lines, a box each: the whole page selected, as pageText selects it. Internal: the editor answers
  // with the boxes it kept from an earlier selection, another page's too, which SetSelection leaves; no builder call
  // clears them.
  function lineBoxes(page) {
    doc.Document.GetFile().removeSelection();
    page.SetSelection({ x: 0.01, y: 0.01 }, { x: 100000, y: 100000 });
    return page.GetSelectionQuads().map(box);
  }

  // The first place, of w by h points, beside the words at `match` that lies inside the page and covers no text line
  // and nothing `taken` (the page's annotations and fields): right of their line, in the left margin, below the line,
  // above it. Null when none is free.
  function placeBeside(pageW, pageH, match, lines, taken, w, h) {
    var cx = (match[0] + match[2]) / 2, cy = (match[1] + match[3]) / 2;
    var line = lines.filter(function (l) { return l[0] <= cx && cx <= l[2] && l[1] <= cy && cy <= l[3]; })[0] || match;
    var gap = 8, top = clamp(match[1] - 2, 0, pageH - h), covered = lines.concat(taken);
    var candidates = [
      [line[2] + gap, top, line[2] + gap + w, top + h],
      [line[0] - gap - w, top, line[0] - gap, top + h],
      [match[0], line[3] + gap, match[0] + w, line[3] + gap + h],
      [match[0], line[1] - gap - h, match[0] + w, line[1] - gap],
    ].map(function (rect) { return rect.map(tenth); });
    return candidates.filter(function (rect) {
      return rect[0] >= 0 && rect[1] >= 0 && rect[2] <= pageW && rect[3] <= pageH && !covered.some(function (b) { return overlaps(rect, b); });
    })[0] || null;
  }

  // A note's box: 200 points wide, a 14-point line for every 192 points of its text at about 5.5 points a letter, and
  // 8 more; at most 300 points tall.
  function noteSize(text) { return [200, Math.min(300, 14 * Math.ceil(text.length * 5.5 / 192) + 8)]; }
  // A stamp's size, the editor's own for its kind (the builder's stamp takes only its top-left corner). Internal: the
  // editor's drawing of the stamp, in millimetres; it changes nothing.
  function stampSize(type, author) {
    var drawn = doc.Document.CreateStampRender(type, author);
    return [drawn.Width * 72 / 25.4, drawn.Height * 72 / 25.4];
  }

  // The nth match of an anchor's words, counted through the pages in order as the op finds them (`order` holds each
  // page's index before the batch, -1 for a page the batch adds): its page number then, its index, its box.
  function findWords(anchor, index, order) {
    var seen = 0;
    for (var n = 0; n < order.length; n++) {
      if (order[n] < 0) continue;   // a page the batch adds is blank
      var quads = doc.GetPage(order[n]).Search({ text: anchor.find, matchCase: anchor.match_case, wholeWords: false });
      if (seen + quads.length >= anchor.nth) return { page: n + 1, source: order[n], match: box(quads[anchor.nth - seen - 1]) };
      seen += quads.length;
    }
    if (!seen) throw new Refusal(index, "\"" + anchor.find + "\" is not in the document.");
    throw new Refusal(index, "\"" + anchor.find + "\" is in the document " + seen + " times: nth counts from 1 up to " + seen + ".");
  }

  // An annotate placed by its words gets its page here, and the place of `size` beside them, clear of what the page
  // holds and of what the batch's earlier ops put there (`added`, by page index before the batch).
  function placeByWords(op, index, order, added, size) {
    var found = findWords(op.anchor, index, order), page = doc.GetPage(found.source), pageSize = sizeOf(found.source);
    var taken = page.GetAllAnnots().map(function (a) { return a.GetRect(); })
      .concat(page.GetAllWidgets().map(function (w) { return w.GetRect(); }), added[found.source] || []);
    var rect = placeBeside(pageSize[0], pageSize[1], found.match, lineBoxes(page), taken, size[0], size[1]);
    if (!rect) throw new Refusal(index, "There is no free place beside \"" + op.anchor.find + "\" on page " + found.page + ": give \"page\" and \"rect\".");
    op.page = found.page;
    return rect;
  }

  // --- the batch

  function Refusal(op, reason) { this.op = op; this.reason = reason; }

  function pendingMarks() {
    for (var i = 0; i < doc.GetPagesCount(); i++) {
      if (doc.GetPage(i).GetAllAnnots().some(function (a) { return a.GetClassType() === "redactAnnot"; })) return true;
    }
    return false;
  }
  function holds(text, matchCase) {
    for (var i = 0; i < doc.GetPagesCount(); i++) {
      var page = pageText(doc.GetPage(i));
      if ((matchCase ? page : page.toLowerCase()).indexOf(matchCase ? text : text.toLowerCase()) >= 0) return true;
    }
    return false;
  }

  // Every refusal before any change, against the document as each op will find it (an added page counts for the
  // ops after it): a refused batch changes nothing, and so needs no Undo. A note placed by its words gets its page and
  // rect here.
  function check(ops, agent) {
    var count = doc.GetPagesCount(), original = count, fields = {}, order = [], added = {};
    for (var i = 0; i < count; i++) order.push(i);
    doc.GetAllFields().forEach(function (f) { fields[f.GetFullName()] = f.GetClassType(); });
    function take(n, rect) {
      var source = order[n - 1];
      if (source >= 0) (added[source] = added[source] || []).push(rect);
    }
    ops.forEach(function (op, index) {
      function page(n) { if (n < 1 || n > count) throw new Refusal(index, "There is no page " + n + ": at this op the document has " + count + "."); }
      function inside(n, rect) {
        if (n > original) return;   // a page this batch adds takes its neighbour's size
        var size = sizeOf(n - 1);
        if (rect[2] > size[0] + 0.5 || rect[3] > size[1] + 0.5) throw new Refusal(index, "The rect reaches past page " + n + ", which is " + size[0] + " by " + size[1] + " points.");
      }
      if (op.op === "pages") {
        if (op.action === "add") {
          if (op.page > count) throw new Refusal(index, "There is no page " + op.page + " to add after: at this op the document has " + count + ".");
          count++;
          order.splice(op.page, 0, -1);
        } else {
          page(op.page);
          if (op.action === "remove") {
            if (count === 1) throw new Refusal(index, "A PDF keeps at least one page.");
            count--;
            order.splice(op.page - 1, 1);
          }
        }
      } else if (op.op === "fill") {
        Object.keys(op.fields).forEach(function (field) {
          var type = fields[field];
          if (!type) throw new Refusal(index, "There is no field " + field + ". The fields are: " + Object.keys(fields).join(", ") + ".");
          var checkbox = type === "checkboxField";
          if (checkbox !== (typeof op.fields[field] === "boolean")) throw new Refusal(index, field + (checkbox ? " is a checkbox: give it true or false." : " is a text field: give it text."));
        });
        // The user's edit wins: a field that changed since the agent read it is left as it is.
        Object.keys(op.expect || {}).forEach(function (field) {
          var f = doc.GetFieldByName(field), now = typeof op.expect[field] === "boolean" ? f.GetAllWidgets()[0].IsChecked() : String(f.GetValue() == null ? "" : f.GetValue());
          if (now !== op.expect[field]) throw new Refusal(index, "Field " + field + " changed since you read it: it is " + JSON.stringify(now) + " now. Work from it.");
        });
      } else if (op.op === "add_field") {
        page(op.page);
        inside(op.page, op.rect);
        if (fields[op.name]) throw new Refusal(index, "A field named " + op.name + " exists.");
        fields[op.name] = op.type === "checkbox" ? "checkboxField" : "textField";
        take(op.page, op.rect);
      } else if (op.op === "annotate") {
        // A stamp takes the room it can grow into: STAMP_GROWTH a side beyond its size, its corner that far in.
        var stamp = op.kind === "stamp" ? stampSize(op.stamp, agent.name) : null, grow = STAMP_GROWTH;
        if (op.anchor) {
          var at = placeByWords(op, index, order, added, stamp ? [stamp[0] + 2 * grow, stamp[1] + 2 * grow] : noteSize(op.text));
          op.rect = stamp ? [at[0] + grow, at[1] + grow, at[0] + grow + stamp[0], at[1] + grow + stamp[1]].map(tenth) : at;
        } else {
          page(op.page);
          inside(op.page, op.rect);
        }
        take(op.page, stamp ? [op.rect[0] - grow, op.rect[1] - grow, op.rect[0] + stamp[0] + grow, op.rect[1] + stamp[1] + grow] : op.rect);
      } else if (op.op === "redact") {
        // Applying a redaction applies every unapplied mark in the document, the person's too.
        if (pendingMarks()) throw new Refusal(index, "The document has redaction marks someone made and has not applied: applying this redaction would apply theirs too. Ask the user to apply or remove them first.");
        if (!holds(op.text, op.match_case)) throw new Refusal(index, "\"" + op.text + "\" is not in the document.");
      }
    });
  }

  // The editor takes no coordinate of 0.
  function corners(rect) { return rect.map(function (n) { return Math.max(n, 0.01); }); }

  // Internal: the builder's SetValue commits a text through an editor action. With a co-editor connected, an action
  // asks the server for the lock on what it changed and refuses every later action until the answer comes, which it
  // cannot during a command: only a batch's first text would land. No builder call commits otherwise.
  function setText(field, value) {
    var target = field.Field.IsWidget() ? field.Field : field.Field.GetKid(0);
    target.SetValue(value);
    if (!doc.Document.private_CommitField(target)) throw new Error("the editor did not take the value of field " + field.GetFullName());
  }

  var APPLY = {
    pages: function (op, index) {
      if (op.action === "add") { doc.AddPage(op.page); return { op: index, page: op.page + 1 }; }
      if (op.action === "remove") doc.RemovePage(op.page - 1);
      else doc.GetPage(op.page - 1).SetRotation(op.degrees);
      return { op: index };
    },
    fill: function (op, index) {
      Object.keys(op.fields).forEach(function (field) {
        var f = doc.GetFieldByName(field), value = op.fields[field];
        if (typeof value === "boolean") f.GetAllWidgets()[0].SetChecked(value);
        else setText(f, value);
      });
      return { op: index };
    },
    add_field: function (op, index) {
      var field = op.type === "checkbox" ? Api.CreateCheckboxField(corners(op.rect)) : Api.CreateTextField(corners(op.rect));
      field.SetFullName(op.name);   // before it joins the page, or it takes a name like Text1
      doc.GetPage(op.page - 1).AddObject(field);
      if (op.value !== undefined) {
        if (op.type === "checkbox") field.GetAllWidgets()[0].SetChecked(op.value);
        else setText(field, op.value);
      }
      return { op: index };
    },
    redact: function (op, index) {
      var marks = doc.SearchAndRedact({ text: op.text, matchCase: op.match_case, wholeWords: op.whole_words });
      if (!marks.length) throw new Error("the editor found nothing to redact");
      doc.ApplyRedact();
      return { op: index, redacted: marks.length };
    },
    // The result says where the note or stamp landed, as the editor holds it, and which words it sits beside.
    annotate: function (op, index, agent) {
      var page = doc.GetPage(op.page - 1), made;
      if (op.kind === "stamp") {
        made = Api.CreateStampAnnot(corners(op.rect), op.stamp, agent.name);
        page.AddObject(made);
      } else {
        var run = Api.CreateRichRun();
        made = Api.CreateFreeTextAnnot(corners(op.rect));
        run.AddText(op.text);
        made.GetContent().GetElement(0).AddElement(run, 0);
        made.SetAuthorName(agent.name);
        page.AddObject(made);
        // Internal: the editor commits a free text's rich text when its box loses focus, which a builder edit never
        // does, and no builder call commits it: without this the saved note is an empty box.
        var impl = made.private_GetImpl();
        impl.SetRichContents(impl.GetRichContents(true));
        made.SetContents(op.text);
      }
      var out = { op: index, page: op.page, rect: made.GetRect().map(tenth) };
      if (op.anchor) out.beside = op.anchor.find;
      return out;
    },
  };

  function edit(ops, agent) {
    check(ops, agent);
    return { results: ops.map(function (op, index) { return APPLY[op.op](op, index, agent); }) };
  }

  try {
    if (scope.action === "outline") return outline(scope.start || 0, scope.limit || 100);
    if (scope.action === "page") return view(scope.page);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "thread") return threadOf(scope.query, scope.author);
    if (scope.action === "edit") return onlyComments(scope.ops) ? comment(scope.ops, scope.agent) : edit(scope.ops, scope.agent);
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    if (e instanceof Refusal) return { refused: { op: e.op, reason: e.reason } };
    return { __error: String((e && e.message) || e) };
  }
})
