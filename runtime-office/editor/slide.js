// The script runtime-office runs in a slide editor through its connector, with `scope` set to the request. It runs
// in the editor's command sandbox (strict mode, no window or document; `Asc` is the connector's own variable, so
// classes come from AscFormat). Every exception is caught here, because the editor swallows them. runtime-office runs an
// edit inside a group of actions, which it cancels when the edit is refused or fails. A slide's id is its internal one,
// the same in every co-editor's editor and kept when other slides are added, moved or removed. Slides take no locks in
// fast co-editing, so a replace or a remove is guarded by the slide's content hash instead. Internal names are used
// only where the builder has no call; each says so.
(function () {
  "use strict";
  var pres = Api.GetPresentation(), VIEW_BUDGET = 30000, MAX_MATCHES = 50, EMU_PER_PT = 12700;

  function hash(text) {
    var h = 0x811c9dc5;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
  }
  function clip(text, max) { return text.length > max ? text.slice(0, max) + " …" : text; }
  function emuPerPx() { return pres.GetWidth() / 1920; }
  function px(emu) { return Math.round(emu / emuPerPx()); }
  function hex(rgba) { return "#" + [rgba.R, rgba.G, rgba.B].map(function (v) { return ("0" + (v | 0).toString(16)).slice(-2); }).join(""); }
  function rgb(text) { var n = parseInt(text.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }

  function idOf(slide) { return String(slide.Slide.Get_Id()); }   // internal: the builder gives slides no id
  function slideById(id) {
    var all = pres.GetAllSlides();
    for (var i = 0; i < all.length; i++) if (idOf(all[i]) === String(id)) return all[i];
    return null;   // Api.GetByInternalId does not find slides
  }

  // A drawing's text, read without making a text body for a shape that has none (GetDocContent would make one).
  function textOf(d) {
    var kind = d.GetClassType();
    // internal: the builder cannot tell whether a shape has a text body
    if (kind === "shape") return d.Shape && d.Shape.txBody ? String(d.GetDocContent().GetText() || "").replace(/\s+$/, "") : "";
    if (kind === "table") {
      var parts = [];
      for (var r = 0; r < d.Table.Content.length; r++) {   // internal: the builder has no row count
        var row = d.GetRow(r);
        for (var c = 0; c < row.GetCellsCount(); c++) parts.push(String(row.GetCell(c).GetContent().GetText() || "").replace(/\s+$/, ""));
      }
      return parts.join("\t");
    }
    return "";
  }
  // The connectors a person draws are no "drawings" to the builder: they are read from the shape tree (internal).
  function connectorsOf(slide) {
    return slide.Slide.cSld.spTree.filter(function (sp) { return sp instanceof AscFormat.CConnectionShape; });
  }
  function notesOf(slide) { var page = slide.GetNotesPage(); return page ? String(page.GetBodyShapeText() || "").replace(/\s+$/, "") : ""; }
  function slideHash(slide) {
    var parts = slide.GetAllDrawings().map(function (d) { return [d.GetClassType(), px(d.GetPosX()), px(d.GetPosY()), px(d.GetWidth()), px(d.GetHeight()), textOf(d)].join("|"); });
    // internal: a connector's frame, which the builder cannot reach
    connectorsOf(slide).forEach(function (sp) { var x = sp.spPr && sp.spPr.xfrm; parts.push(["connector", x ? [x.offX, x.offY, x.extX, x.extY].map(Math.round).join(",") : ""].join("|")); });
    parts.push(notesOf(slide));
    return hash(parts.join("\n"));
  }
  function titleOf(slide) {
    var drawings = slide.GetAllDrawings(), first = "";
    for (var i = 0; i < drawings.length; i++) {
      var placeholder = drawings[i].GetPlaceholder ? drawings[i].GetPlaceholder() : null, type = placeholder ? placeholder.GetType() : "";
      var text = textOf(drawings[i]).replace(/\s+/g, " ").trim();
      if (type === "title" || type === "ctrTitle") return clip(text, 80);
      if (!first && text) first = text;
    }
    return clip(first, 80);
  }
  function entry(slide) {
    var layout = slide.GetLayout();
    return { id: idOf(slide), number: slide.GetSlideIndex() + 1, title: titleOf(slide), layout: layout ? layout.GetName() : "",
      shapes: slide.GetAllDrawings().length + connectorsOf(slide).length, notes: !!notesOf(slide), hidden: !slide.GetVisible(), hash: slideHash(slide) };
  }

  // --- reading

  function outline() {
    return { canvas: [1920, Math.round(1920 * pres.GetHeight() / pres.GetWidth())], slides: pres.GetAllSlides().map(entry) };
  }

  function fillText(uni) {   // a compiled fill (internal: the builder reads only direct formatting)
    var f = uni && uni.fill;
    if (!f || f instanceof AscFormat.CNoFill) return "none";
    if (f instanceof AscFormat.CSolidFill) return f.color && f.color.RGBA ? hex(f.color.RGBA) : "solid";
    return f instanceof AscFormat.CBlipFill ? "picture" : f instanceof AscFormat.CGradFill ? "gradient" : "pattern";
  }
  function runOf(r, href) {
    var text = String(r.GetText()).replace(/[\r\n\t]+$/, "");
    if (!text) return null;
    var cp = r.Run.Get_CompiledPr(true), out = { text: text };   // internal: the builder reads only direct formatting
    if (cp.Bold) out.b = true;
    if (cp.Italic) out.i = true;
    if (cp.Underline) out.u = true;
    out.size = Math.round(cp.FontSize * EMU_PER_PT / emuPerPx());   // points to canvas px
    out.font = cp.RFonts && cp.RFonts.Ascii ? cp.RFonts.Ascii.Name : null;
    // A theme colour resolves in Unifill; Color stays black.
    out.color = cp.Unifill && cp.Unifill.fill && cp.Unifill.fill.color ? hex(cp.Unifill.fill.color.RGBA) : hex({ R: cp.Color.r, G: cp.Color.g, B: cp.Color.b });
    if (href) out.href = href;
    return out;
  }
  function parasOf(content) {
    var out = [];
    for (var k = 0; k < content.GetElementsCount(); k++) {
      var p = content.GetElement(k);
      if (p.GetClassType() !== "paragraph") continue;
      var runs = [];
      for (var j = 0; j < p.GetElementsCount(); j++) {
        var el = p.GetElement(j), type = el.GetClassType(), run;
        if (type === "run" && (run = runOf(el, null))) runs.push(run);
        else if (type === "hyperlink") for (var h = 0; h < el.GetElementsCount(); h++) if ((run = runOf(el.GetElement(h), el.GetLinkedText()))) runs.push(run);
      }
      // internal: the builder reads neither bullets nor levels
      var para = { align: p.GetJc() || "left", runs: runs }, pr = p.Paragraph.Get_CompiledPr2(false).ParaPr;
      if (pr.Bullet && pr.Bullet.bulletType) {
        if (pr.Bullet.bulletType.type === 1) para.bullet = pr.Bullet.bulletType.Char;
        else if (pr.Bullet.bulletType.type === 2) para.numbered = true;
      }
      if (pr.Lvl) para.level = pr.Lvl;
      out.push(para);
    }
    return out;
  }

  // One slide in full, in canvas px; deeper than a command's answer may nest, so it goes back as JSON.
  function view(id) {
    var slide = slideById(id);
    if (!slide) return { error: "No slide " + id + ": read the outline again." };
    var drawings = slide.GetAllDrawings(), shapes = [], size = 0;
    for (var i = 0; i < drawings.length; i++) {
      var d = drawings[i], kind = d.GetClassType();
      // A picture from an SVG may say "drawing".
      var shape = { id: String(d.GetInternalId()), kind: kind === "drawing" ? "image" : kind, x: px(d.GetPosX()), y: px(d.GetPosY()), w: px(d.GetWidth()), h: px(d.GetHeight()) };
      if (d.GetName && d.GetName()) shape.name = d.GetName();
      if (d.GetRotation && d.GetRotation()) shape.rotation = d.GetRotation();
      var placeholder = d.GetPlaceholder ? d.GetPlaceholder() : null;
      if (placeholder) shape.placeholder = placeholder.GetType();
      if (kind === "shape") {
        // internal: the compiled fill and line, theme and style resolved; the builder reads only direct ones
        shape.fill = fillText(d.Shape.brush);
        shape.line = d.Shape.pen ? fillText(d.Shape.pen.Fill) : "none";
        if (d.Shape.txBody) shape.paras = parasOf(d.GetDocContent());
      } else if (kind === "table") {
        shape.rows = [];
        for (var r = 0; r < d.Table.Content.length; r++) {   // internal: the builder has no row count
          var row = d.GetRow(r), cells = [];
          for (var c = 0; c < row.GetCellsCount(); c++) cells.push(parasOf(row.GetCell(c).GetContent()));
          shape.rows.push(cells);
        }
      }
      size += JSON.stringify(shape).length;
      if (size > VIEW_BUDGET) { shapes.push({ truncated: true }); break; }
      shapes.push(shape);
    }
    connectorsOf(slide).forEach(function (sp) {
      // internal: a connector's frame, in mm, which the builder cannot reach
      var x = sp.spPr && sp.spPr.xfrm, mmPerPx = emuPerPx() / 36000;
      shapes.push({ kind: "connector", x: x ? Math.round(x.offX / mmPerPx) : null, y: x ? Math.round(x.offY / mmPerPx) : null, w: x ? Math.round(x.extX / mmPerPx) : null, h: x ? Math.round(x.extY / mmPerPx) : null });
    });
    return { __json: JSON.stringify({ id: idOf(slide), number: slide.GetSlideIndex() + 1, hidden: !slide.GetVisible(), hash: slideHash(slide), shapes: shapes, notes: notesOf(slide) }) };
  }

  function search(query) {
    var q = String(query).toLowerCase(), matches = [], total = 0;
    pres.GetAllSlides().forEach(function (slide) {
      var texts = slide.GetAllDrawings().map(textOf);
      texts.push(notesOf(slide));
      texts.forEach(function (text) {
        var at = text.toLowerCase().indexOf(q);
        if (at < 0) return;
        total++;
        if (matches.length < MAX_MATCHES) matches.push({ id: idOf(slide), number: slide.GetSlideIndex() + 1, text: clip(text.slice(Math.max(0, at - 60)).replace(/\s+/g, " "), 200) });
      });
    });
    return { matches: matches, total: total };
  }

  // --- building. The order of the calls below matters: each trap named was met in the editor.

  // A fill: solid, with its alpha as the fill's own transparency.
  function fillOf(fill) {
    if (!fill) return Api.CreateNoFill();
    var c = rgb(fill.color.hex), f = Api.CreateSolidFill(Api.CreateRGBColor(c[0], c[1], c[2]));
    if (fill.color.alpha < 1) f.UniFill.transparent = Math.round(fill.color.alpha * 255);   // internal: Api.RGBA's alpha is dropped
    return f;
  }
  function strokeOf(stroke) {
    if (!stroke) return Api.CreateStroke(0, Api.CreateNoFill());
    return Api.CreateStroke(stroke.w, fillOf({ type: "solid", color: stroke.color }));
  }
  function styleRun(run, r) {
    if (r.b) run.SetBold(true);
    if (r.i) run.SetItalic(true);
    if (r.u) run.SetUnderline(true);
    run.SetFontSize(Math.round(r.size * 2));   // half-points
    if (r.font) run.SetFontFamily(r.font);
    var c = rgb(r.color.hex);
    run.SetColor(c[0], c[1], c[2]);
  }
  function addText(run, text) {
    var lines = String(text).split("\n");
    for (var k = 0; k < lines.length; k++) {
      if (k > 0) run.AddLineBreak();
      if (lines[k]) run.AddText(lines[k]);
    }
  }
  function paragraphOf(pp) {
    var p = Api.CreateParagraph();
    for (var k = 0; k < pp.runs.length; k++) {
      var r = pp.runs[k], run = Api.CreateRun();
      addText(run, r.text);
      STYLE_RUN.forEach(function (style) { style(run, r); });
      if (r.href) {
        // SetDisplayedText throws in the slide editor: the run goes into the link itself (internal: no builder call
        // puts a run in a link).
        var link = Api.CreateHyperlink(r.href, "");
        link.ParaHyperlink.Add_ToContent(0, run.Run, false);
        p.AddElement(link);
      } else {
        p.AddElement(run);
      }
    }
    p.SetJc(pp.align || "left");
    p.SetSpacingLine(Math.round((pp.line || 1) * 240), "auto");   // a multiple of single
    p.SetSpacingBefore(Math.round((pp.before || 0) * 20));        // points to twips
    p.SetSpacingAfter(Math.round((pp.after || 0) * 20));
    if (pp.bullet) p.SetBullet(Api.CreateBullet(pp.bullet));
    else if (pp.numbered) p.SetBullet(Api.CreateNumbering("ArabicPeriod", pp.start || 1));
    else p.SetBullet(Api.CreateBullet(""));
    if (pp.indent !== undefined) {
      p.SetIndLeft(pp.indent);
      p.SetIndFirstLine(pp.hanging ? -pp.hanging : 0);
    }
    // Last: each ApiParagraph setter writes its whole paragraph properties back, which hold no level (internal: the
    // builder's SetOutlineLvl is Word's outline level, not a slide paragraph's).
    if (pp.level) p.Paragraph.Set_PresentationLevel(pp.level);
    return p;
  }
  // Run styles, each applied in turn; more are added beside this one.
  var STYLE_RUN = [styleRun];
  function fillContent(content, paras) {
    content.RemoveAllElements();   // leaves one empty paragraph
    for (var i = 0; i < paras.length; i++) content.Push(paragraphOf(paras[i]));
    content.RemoveElement(0);
  }
  function plainTableStyle() {   // internal: the builder names no table style
    var styles = pres.Presentation.globalTableStyles.Style;
    for (var id in styles) if (styles[id].Name === "No Style, No Grid") return id;
    return pres.Presentation.DefaultTableStyleId;
  }
  // A shape's geometry; a rectangle is what CreateShape made.
  function geometryOf(shape, spec) { return shape; }
  function placed(drawing, s) {
    drawing.SetPosition(s.x, s.y);
    return drawing;
  }
  var BUILD = {
    shape: function (s) {
      // A CSS border is inside its box; an OOXML line straddles its edge: the geometry is inset by half the width, and
      // the text keeps its place with the other half added to its insets.
      var half = s.stroke ? s.stroke.w / 2 : 0;
      var sh = Api.CreateShape("rect", Math.max(1, s.w - 2 * half), Math.max(1, s.h - 2 * half), fillOf(s.fill), strokeOf(s.stroke));
      sh.Shape.setStyle(null);   // internal: CreateShape gives the theme's style, a shadow and white text, and no call takes it off
      geometryOf(sh, s);
      if (s.text) {
        var content = sh.GetDocContent();   // makes the text body: insets and anchor need it
        var i = s.text.insets;
        sh.SetPaddings(i[0] + half, i[1] + half, i[2] + half, i[3] + half);
        sh.SetVerticalTextAlign(s.text.anchor === "ctr" ? "center" : s.text.anchor === "b" ? "bottom" : "top");
        fillContent(content, s.text.paras);
        if (!s.text.wrap) {   // internal: the builder has no wrap setting
          var body = sh.Shape.getBodyPr().createDuplicate();
          body.wrap = AscFormat.nTWTNone;
          sh.Shape.txBody.setBodyPr(body);
        }
      }
      return placed(sh, { x: s.x + half, y: s.y + half });
    },
    picture: function (s) {
      var picture = Api.CreateImage(s.src, s.w, s.h);
      // Set before the picture joins the slide: a crop changed later inside a command is never sent (internal: the
      // builder has no crop).
      if (s.crop) {
        var fill = picture.Drawing.blipFill.createDuplicate();
        fill.srcRect = new AscFormat.CSrcRect(s.crop[0], s.crop[1], 100 - s.crop[2], 100 - s.crop[3]);   // edge positions
        picture.Drawing.setBlipFill(fill);
      }
      return placed(picture, s);
    },
    table: function (s) {
      var t = Api.CreateTable(s.cols.length, s.rows.length), T = t.Table, mm = s.cols.map(function (w) { return w / 36000; });
      T.Set_TableStyle(plainTableStyle());   // internal, and first: it clears direct formatting
      t.SetTableLook(false, false, false, false, false, false);
      t.SetPosition(s.x, s.y);
      // The grid, and each cell's own width: CreateTable's equal cell widths win over the grid (internal: the builder
      // sets neither).
      T.SetTableGrid(mm);
      for (var r = 0; r < s.rows.length; r++) {
        var row = t.GetRow(r), spec = s.rows[r];
        // A slide row's height is the whole row; ApiTableRow.SetHeight takes margins off it (internal; 0 = at least).
        row.Row.Set_Height(spec.h / 36000, 0);
        for (var c = 0; c < spec.cells.length; c++) {
          var cellSpec = spec.cells[c], cell = row.GetCell(c), pr = cell.Cell.Pr.Copy(), fill = cellSpec.fill || spec.fill;
          pr.TableCellW.W = mm[c];   // internal: the builder sets no cell width
          cell.Cell.Set_Pr(pr);
          // No fill must be said: an unshaded cell takes the table style's white. A row's background is each cell's.
          if (fill) { var f = rgb(fill.hex); cell.SetShd("clear", f[0], f[1], f[2]); } else cell.SetShd("nil");
          if (cellSpec.border) {
            var bf = fillOf({ type: "solid", color: cellSpec.border.color }), mmWidth = cellSpec.border.w / 36000;
            cell.SetCellBorderTop(mmWidth, bf); cell.SetCellBorderBottom(mmWidth, bf);
            cell.SetCellBorderLeft(mmWidth, bf); cell.SetCellBorderRight(mmWidth, bf);
          }
          cell.SetCellMarginTop(s.pad[0]); cell.SetCellMarginRight(s.pad[1]);
          cell.SetCellMarginBottom(s.pad[2]); cell.SetCellMarginLeft(s.pad[3]);
          fillContent(cell.GetContent(), cellSpec.paras);
        }
      }
      // The saved frame size is not recalculated from the grid: set it, or it keeps CreateTable's width (internal: the
      // builder's SetSize does not reach the saved frame).
      var sum = function (list) { return list.reduce(function (a, b) { return a + b; }, 0); };
      t.Drawing.spPr.xfrm.setExtX(sum(s.cols) / 36000);
      t.Drawing.spPr.xfrm.setExtY(sum(s.rows.map(function (row) { return row.h; })) / 36000);
      return t;
    },
  };
  function blankLayout() { var master = pres.GetMaster(0); return master ? master.GetLayoutByType("blank") : null; }
  // Run after each shape joins its slide, in turn.
  var AFTER_ADD = [];
  function build(slide, spec) {
    if (spec.background) slide.SetBackground(fillOf(spec.background));
    for (var i = 0; i < spec.shapes.length; i++) {
      var maker = BUILD[spec.shapes[i].kind];
      if (!maker) throw new Error("no builder for a " + spec.shapes[i].kind);
      var drawing = maker(spec.shapes[i]);
      if (!slide.AddObject(drawing)) throw new Error("the slide refused shape " + i);
      for (var a = 0; a < AFTER_ADD.length; a++) AFTER_ADD[a](slide, drawing, spec.shapes[i]);
    }
    if (spec.notes) slide.AddNotesText(spec.notes);
    slide.SetVisible(!spec.hidden);
  }
  function added(after, spec) {
    var slide = Api.CreateSlide(), blank = blankLayout();
    if (blank) slide.ApplyLayout(blank);
    slide.RemoveAllObjects();
    var index = after === null ? pres.GetSlidesCount() : after === "start" ? 0 : slideById(after).GetSlideIndex() + 1;
    pres.AddSlide(slide, index);   // the new slide becomes current: CreateTable needs one
    build(slide, spec);
    return slide;
  }
  function replaced(slide, spec) {
    slide.RemoveAllObjects();   // its builds go with its shapes
    var blank = blankLayout();
    if (blank) slide.ApplyLayout(blank);
    var notes = slide.GetNotesPage();
    if (notes && notes.GetBodyShape()) notes.GetBodyShape().GetDocContent().RemoveAllElements();
    build(slide, spec);
    return slide;
  }

  // --- the batch: every check first, then the changes in order

  function Refusal(op, reason, slide) { this.op = op; this.reason = reason; this.slide = slide ? entry(slide) : undefined; }
  function guarded(target, op) {
    var slide = slideById(target.id);
    if (!slide) throw new Refusal(op, "Slide " + target.id + " is gone: deleted since you read it, or by an earlier op of this call. Read the outline again.");
    if (slideHash(slide) !== target.hash) throw new Refusal(op, "Slide " + target.id + " changed since you read it. This is it now; work from it.", slide);
    return slide;
  }
  function check(ops) {
    var ids = pres.GetAllSlides().map(idOf), removed = {};
    ops.forEach(function (op, index) {
      if (op.op === "slides") {
        op.slides.forEach(function (s) {
          if (s.id !== undefined) {
            if (removed[s.id]) throw new Refusal(index, "Slide " + s.id + " is removed by this call.");
            guarded(s, index);
          } else if (s.after !== null && s.after !== "start" && (ids.indexOf(s.after) < 0 || removed[s.after])) {
            throw new Refusal(index, "There is no slide " + s.after + " to add after: read the outline again.");
          }
        });
      } else if (op.op === "remove") {
        op.slides.forEach(function (s) { guarded(s, index); removed[s.id] = true; });
        if (ids.filter(function (id) { return !removed[id]; }).length === 0) throw new Refusal(index, "A deck keeps at least one slide.");
      } else if (op.op === "order") {
        if (op.ids.slice().sort().join(",") !== ids.slice().sort().join(",")) {
          throw new Refusal(index, "\"order\" lists every slide of the deck once, by id. Its slides are now: " + ids.join(", ") + ".");
        }
      }
    });
  }
  function edit(ops) {
    check(ops);
    return {
      results: ops.map(function (op, index) {
        if (op.op === "slides") {
          var last = {}, slides = op.slides.map(function (s) {
            if (s.id !== undefined) return replaced(slideById(s.id), s.spec);
            // Several slides after one place keep their order: each goes after the one added before it.
            var key = String(s.after), slide = added(last[key] !== undefined ? last[key] : s.after, s.spec);
            last[key] = idOf(slide);
            return slide;
          });
          return { op: index, slides: slides.map(function (slide) { return { id: idOf(slide), number: slide.GetSlideIndex() + 1 }; }) };
        }
        if (op.op === "remove") {
          op.slides.forEach(function (s) { slideById(s.id).Delete(); });
          return { op: index };
        }
        op.ids.forEach(function (id, at) { var slide = slideById(id); if (slide.GetSlideIndex() !== at) slide.MoveTo(at); });
        return { op: index, order: pres.GetAllSlides().map(idOf) };
      }),
    };
  }

  try {
    if (scope.action === "size") return { width: pres.GetWidth(), height: pres.GetHeight() };
    if (scope.action === "outline") return outline();
    if (scope.action === "slide") return view(scope.target);
    if (scope.action === "search") return search(scope.query);
    if (scope.action === "edit") return edit(scope.ops);
    return { __error: "unknown action " + scope.action };
  } catch (e) {
    // A builder call that throws inside the editor's no-history section leaves its object registry off: everything
    // made after would be unregistered, the person's editor would drop to view mode and the document would never save.
    // It is only ever off in the middle of such a call, so turning it back on here is always right; runtime-office
    // then cancels the group, and closes the page if it is still off. (internal: the builder has no call for either)
    try {
      AscCommon.g_oTableId.m_bTurnOff = false;
      if (!AscCommon.History.Is_On()) AscCommon.History.TurnOn();
    } catch (ignored) { /* the answer below is what the agent needs */ }
    if (e instanceof Refusal) return { refused: { op: e.op, reason: e.reason, slide: e.slide } };
    return { __error: String((e && e.message) || e) };
  }
})
