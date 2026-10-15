// The script runtime-office runs in a PDF editor through its connector, with `scope` set to the request. It runs in
// the editor's command sandbox (strict mode, no window or document). Every exception is caught here, because the
// editor swallows them. The PDF editor cannot start a group of actions: a batch is this one command, which decides
// every refusal before it changes anything, and runtime-office undoes it at once if it fails partway. Coordinates are
// points from the page's top-left, y down, as the editor takes them. Internal names are used where the builder has no
// call; each says so.
(function () {
  "use strict";
  var doc = Api.GetDocument(), TEXT_BUDGET = 30000, MAX_MATCHES = 50, MAX_SEARCH_PAGES = 200;

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
      return { name: f.GetFullName(), type: w.GetClassType() === "checkboxWidget" ? "checkbox" : w.GetClassType().replace(/Widget$/, ""),
        value: f.GetParentValue ? (f.GetParentValue() === undefined ? null : f.GetParentValue()) : null, rect: w.GetRect() };
    });
  }
  function annotationsOf(page) {
    return page.GetAllAnnots().map(function (a) {
      return { type: a.GetClassType().replace(/Annot$/, ""), rect: a.GetRect(), author: a.GetAuthorName(), text: a.GetContents() || "" };
    });
  }

  // --- reading

  function outline(start, limit) {
    var count = doc.GetPagesCount(), pages = [];
    for (var i = start; i < count && pages.length < limit; i++) {
      var page = doc.GetPage(i), text = pageText(page);
      pages.push({ page: i + 1, size: sizeOf(i), rotation: page.GetRotation(),
        first_line: clip(text.split("\n").filter(function (l) { return l.trim(); })[0] || "", 120), chars: text.length,
        fields: fieldsOf(page), annotations: annotationsOf(page) });
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
    if (scope.action === "edit") return edit(scope.ops, scope.agent);
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    if (e instanceof Refusal) return { refused: { op: e.op, reason: e.reason } };
    return { __error: String((e && e.message) || e) };
  }
})
