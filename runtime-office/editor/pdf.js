// The script runtime-office runs in a PDF editor through its connector, with `scope` set to the request. It runs in
// the editor's command sandbox (strict mode, no window or document). Every exception is caught here, because the
// editor swallows them. The PDF editor cannot start a group of actions: a batch is this one command, which decides
// every refusal before it changes anything, and runtime-office undoes it at once if it fails partway. Coordinates are
// points from the page's top-left, y down, as the editor takes them. Internal names are used where the builder has no
// call; each says so.
(function () {
  "use strict";
  var doc = Api.GetDocument(), TEXT_BUDGET = 30000, MAX_MATCHES = 50, MAX_SEARCH_PAGES = 200, MAX_REPLIES = 10;

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
      if (!found[op.thread]) throw new Refusal(index, "Comment thread " + op.thread + " is gone: read the outline again.");
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
  // ops after it): a refused batch changes nothing, and so needs no Undo.
  function check(ops) {
    var count = doc.GetPagesCount(), original = count, fields = {};
    doc.GetAllFields().forEach(function (f) { fields[f.GetFullName()] = f.GetClassType(); });
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
        } else {
          page(op.page);
          if (op.action === "remove") {
            if (count === 1) throw new Refusal(index, "A PDF keeps at least one page.");
            count--;
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
      } else if (op.op === "annotate") {
        page(op.page);
        inside(op.page, op.rect);
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
    annotate: function (op, index, agent) {
      var page = doc.GetPage(op.page - 1);
      if (op.kind === "stamp") {
        var stamp = Api.CreateStampAnnot(corners(op.rect), op.stamp, agent.name);
        page.AddObject(stamp);
        return { op: index };
      }
      var note = Api.CreateFreeTextAnnot(corners(op.rect)), run = Api.CreateRichRun();
      run.AddText(op.text);
      note.GetContent().GetElement(0).AddElement(run, 0);
      note.SetAuthorName(agent.name);
      page.AddObject(note);
      // Internal: the editor commits a free text's rich text when its box loses focus, which a builder edit never
      // does, and no builder call commits it: without this the saved note is an empty box.
      var impl = note.private_GetImpl();
      impl.SetRichContents(impl.GetRichContents(true));
      note.SetContents(op.text);
      return { op: index };
    },
  };

  function edit(ops, agent) {
    check(ops);
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
