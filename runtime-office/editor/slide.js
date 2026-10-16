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
  // The heading runtime-office named, else the title placeholder, else the first text.
  function titleOf(slide) {
    var drawings = slide.GetAllDrawings(), title = null, first = "";
    for (var i = 0; i < drawings.length; i++) {
      var placeholder = drawings[i].GetPlaceholder ? drawings[i].GetPlaceholder() : null, type = placeholder ? placeholder.GetType() : "";
      var text = textOf(drawings[i]).replace(/\s+/g, " ").trim();
      if (drawings[i].GetName && drawings[i].GetName() === "Title") return clip(text, 80);
      if (title === null && (type === "title" || type === "ctrTitle")) title = text;
      if (!first && text) first = text;
    }
    return clip(title !== null ? title : first, 80);
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

  function colorOf(c) { var v = rgb(c.hex); return Api.CreateRGBColor(v[0], v[1], v[2]); }
  // A fill: solid, with its alpha as the fill's own transparency, or a linear gradient with its stops' alpha.
  function fillOf(fill) {
    if (!fill) return Api.CreateNoFill();
    if (fill.type === "linear") {
      var stops = fill.stops.map(function (s) {
        var stop = Api.CreateGradientStop(colorOf(s.color), Math.round(s.at * 1000));   // 1/1000 %
        // internal: the builder's colours take no alpha
        if (s.color.alpha < 1) stop.Gs.color.addColorMod(new AscFormat.CColorMod("alpha", Math.round(s.color.alpha * 100000)));
        return stop;
      });
      var f = Api.CreateLinearGradientFill(stops, (((fill.angle - 90) % 360 + 360) % 360) * 60000);   // a CSS angle, in 1/60000 deg
      // internal: the builder leaves the gradient scaled to the box, which only CSS's corner keywords are
      f.UniFill.fill.lin.scale = !!fill.scaled;
      return f;
    }
    var solidFill = Api.CreateSolidFill(colorOf(fill.color));
    if (fill.color.alpha < 1) solidFill.UniFill.transparent = Math.round(fill.color.alpha * 255);   // internal: Api.RGBA's alpha is dropped
    return solidFill;
  }
  // A line: its width, colour and dashes, and the heads at its start and end. The line setters are plain assignments,
  // read when the shape takes the line: they go first.
  function strokeOf(stroke, head, tail) {
    if (!stroke) return Api.CreateStroke(0, Api.CreateNoFill());
    var st = Api.CreateStroke(stroke.w, fillOf({ type: "solid", color: stroke.color }), stroke.dash === "solid" ? undefined : stroke.dash);
    if (stroke.dash === "sysDot") st.Ln.setCap(1);   // internal: round caps make round dots; the builder sets no cap
    if (head) st.Ln.setHeadEnd(end(head));            // internal: the builder sets no line ends
    if (tail) st.Ln.setTailEnd(end(tail));
    return st;
  }
  function end(kind) {
    if (!kind || kind === "none") return null;
    var a = new AscFormat.EndArrow();   // internal, as the line ends above
    a.type = kind === "open" ? AscFormat.LineEndType.Arrow : AscFormat.LineEndType.Triangle;
    a.w = AscFormat.LineEndSize.Mid;
    a.len = AscFormat.LineEndSize.Mid;
    return a;
  }
  // A shape's opacity, which OOXML has no property for, multiplied into its fill's and line's alpha.
  function faded(c, o) { return o === undefined ? c : { hex: c.hex, alpha: c.alpha * o }; }
  function fadedFill(fill, o) {
    if (!fill || o === undefined) return fill;
    if (fill.type === "linear") return { type: "linear", angle: fill.angle, scaled: fill.scaled, stops: fill.stops.map(function (st) { return { color: faded(st.color, o), at: st.at }; }) };
    return { type: "solid", color: faded(fill.color, o) };
  }
  function styleRun(run, r) {
    if (r.b) run.SetBold(true);
    if (r.i) run.SetItalic(true);
    if (r.u) run.SetUnderline(true);
    run.SetFontSize(Math.round(r.size * 2));   // half-points
    if (r.font) run.SetFontFamily(r.font);
    var c = rgb(r.color.hex);
    run.SetColor(c[0], c[1], c[2]);
    if (r.spacing) run.SetSpacing(Math.round(r.spacing * 20));   // points to twips
    if (r.caps) run.SetCaps(true);
    if (r.s) run.SetStrikeout(true);
    if (r.outline) run.SetOutLine(strokeOf(r.outline));
    // SetFill, not SetTextFill: the saved file keeps only SetFill's.
    if (r.gradient) run.SetFill(fillOf(r.gradient));
    else if (r.color.alpha < 1) run.SetFill(fillOf({ type: "solid", color: r.color }));
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
      styleRun(run, r);
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
  // A shape's geometry: a preset with its adjust values, or custom paths in EMU. A rectangle is what CreateShape made.
  function geometryOf(shape, spec) {
    var g = spec.geometry;
    if (!g || g.preset === "rect") return shape;
    if (g.custom) {
      var geometry = Api.CreateCustomGeometry();
      g.custom.forEach(function (path) {
        var p = geometry.AddPath();
        p.SetWidth(path.w);
        p.SetHeight(path.h);
        if (!path.fill) p.SetFill("none");
        if (!path.stroke) p.SetStroke(false);
        path.d.forEach(function (step) {
          if (step[0] === "M") p.MoveTo(step[1], step[2]);
          else if (step[0] === "L") p.LineTo(step[1], step[2]);
          else if (step[0] === "A") p.ArcTo(step[1], step[2], step[3], step[4]);
          else if (step[0] === "Z") p.Close();
        });
      });
      shape.SetGeometry(geometry);
      return shape;
    }
    // Never CreateShape("line") or a connector preset: that makes a connector, which throws once the slide holds shapes
    // added in the same command and leaves the document unsaveable. A line is a rectangle given the line geometry.
    shape.SetGeometry(Api.CreatePresetGeometry(g.preset));
    var current = shape.GetGeometry();
    if (g.adj !== undefined) current.SetAdjValue("adj", g.adj);
    if (g.adj1 !== undefined) { current.SetAdjValue("adj1", g.adj1); current.SetAdjValue("adj2", g.adj2); }
    shape.SetGeometry(current);
    // CreateShape makes a size of 0 an inch: a line's comes after its geometry.
    if (g.preset === "line") shape.SetSize(spec.w, spec.h);
    return shape;
  }
  function placed(drawing, s) {
    drawing.SetPosition(s.x, s.y);
    return drawing;
  }
  // Rotation, flips and an unblurred outer shadow. A negative angle would be saved negative.
  function turned(drawing, s) {
    if (s.rot) drawing.SetRotation(((s.rot % 360) + 360) % 360);
    if (s.flipH) drawing.SetFlipH(true);
    if (s.flipV) drawing.SetFlipV(true);
    if (s.shadow) drawing.Drawing.spPr.setEffectPr(shadowOf(s.shadow, s));   // internal: the builder sets no shadow
    return drawing;
  }
  // internal: the builder makes no shadow. Blur 0, as the editor draws every shadow (blurred ones are pictures); the
  // spread grows it about its centre.
  function shadowOf(sh, s) {
    var list = new AscFormat.CEffectLst(), e = new AscFormat.COuterShdw(), c = rgb(sh.color.hex), u = AscFormat.CreateUniColorRGB(c[0], c[1], c[2]);
    var alpha = faded(sh.color, s.opacity).alpha;
    if (alpha < 1) u.addColorMod(new AscFormat.CColorMod("alpha", Math.round(alpha * 100000)));
    e.color = u;
    e.blurRad = 0;
    e.dist = Math.round(Math.sqrt(sh.x * sh.x + sh.y * sh.y));
    e.dir = Math.round(((Math.atan2(sh.y, sh.x) * 180 / Math.PI) + 360) % 360 * 60000);
    e.algn = AscFormat.RECT_ALIGN_CTR;
    e.rotWithShape = false;
    if (sh.spread) { e.sx = Math.round((s.w + 2 * sh.spread) / s.w * 100000); e.sy = Math.round((s.h + 2 * sh.spread) / s.h * 100000); }
    list.outerShdw = e;
    var pr = new AscFormat.CEffectProperties();
    pr.EffectLst = list;
    return pr;
  }
  var BUILD = {
    shape: function (s) {
      // A CSS border is inside its box; an OOXML line straddles its edge: the geometry is inset by half the width, and
      // the text keeps its place with the other half added to its insets. A line shape is its line.
      var line = s.geometry && s.geometry.preset === "line", half = s.stroke && !line ? s.stroke.w / 2 : 0;
      var stroke = s.stroke && { w: s.stroke.w, color: faded(s.stroke.color, s.opacity), dash: s.stroke.dash };
      var sh = Api.CreateShape("rect", Math.max(1, s.w - 2 * half), Math.max(1, s.h - 2 * half), fillOf(fadedFill(s.fill, s.opacity)), strokeOf(stroke));
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
      return turned(placed(sh, { x: s.x + half, y: s.y + half }), s);
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
      // internal, and as early: the builder gives a picture neither a geometry nor a transparency
      if (s.radius) { var g = AscFormat.CreateGeometry("roundRect"); g.setAdjValue("adj", s.radius); picture.Drawing.spPr.setGeometry(g); }
      if (s.opacity !== undefined) picture.Drawing.setTransparent(Math.round(s.opacity * 255));
      return turned(placed(picture, s), s);
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
      return t;   // a table cannot turn: a turned one was measured as a picture
    },
    // A connector: a straight line is the line preset over its box, flipped to run from its first point to its last; a
    // routed one an open path through its corners. Its heads are at its two ends.
    path: function (s) {
      var pts = s.points, a = pts[0], b = pts[pts.length - 1];
      var shape = Api.CreateShape("rect", Math.max(1, s.w), Math.max(1, s.h), Api.CreateNoFill(), strokeOf(s.stroke, s.head, s.tail));
      shape.Shape.setStyle(null);   // internal, as for shapes
      if (pts.length === 2) {
        shape.SetGeometry(Api.CreatePresetGeometry("line"));
        if (b[0] < a[0]) shape.SetFlipH(true);
        if (b[1] < a[1]) shape.SetFlipV(true);
      } else {
        var geometry = Api.CreateCustomGeometry(), p = geometry.AddPath();
        p.SetWidth(s.w);
        p.SetHeight(s.h);
        p.SetFill("none");
        pts.forEach(function (pt, i) { if (i) p.LineTo(pt[0] - s.x, pt[1] - s.y); else p.MoveTo(pt[0] - s.x, pt[1] - s.y); });
        shape.SetGeometry(geometry);
      }
      shape.SetSize(s.w, s.h);   // after the geometry: CreateShape makes a size of 0 an inch
      return placed(shape, s);
    },
    group: function (s) {
      return Api.CreateGroup(s.children.map(function (child) {
        var drawing = BUILD[child.kind](child);
        // CreateGroup places each drawing by its computed place, which the editor computes for a group only once it is
        // on a slide: a group grouped again would count as at the slide's corner, and land there. (internal: the
        // builder has no call that computes it)
        if (child.kind === "group") drawing.Drawing.recalculateTransform();
        return drawing;
      }));
    },
  };
  function blankLayout() { var master = pres.GetMaster(0); return master ? master.GetLayoutByType("blank") : null; }
  // The shapes whose builds go on their slide once all its shapes are on it.
  var pendingBuilds = [];
  // Builds, by order and then as listed. Fade, scale and pop are public effects; the fly-ins take a direction (a preset
  // subtype) AddEffect does not pass, so they take its own three internal steps with it.
  // A fly-in's subtype is where the shape comes from, a fly-out's where it goes: a rise comes up from the bottom and
  // leaves by the top, a drop the reverse.
  var FLY = { 1: { rise: 4, drop: 1, left: 8, right: 2 }, 2: { rise: 1, drop: 4, left: 8, right: 2 } };
  var PUBLIC = { fade: ["fade", "fade-out"], scale: ["zoom", "zoom-out"], pop: ["bounce", "bounce-out"] };
  function addBuilds(slide) {
    var mine = pendingBuilds.filter(function (b) { return b.slide === slide; }), steps = [];
    pendingBuilds = pendingBuilds.filter(function (b) { return b.slide !== slide; });
    mine.forEach(function (b, i) {
      if (b.build.in) steps.push({ drawing: b.drawing, step: b.build.in, cls: 1, at: i });
      if (b.build.out) steps.push({ drawing: b.drawing, step: b.build.out, cls: 2, at: i });
    });
    steps.sort(function (a, b) { return (a.step.order || 0) - (b.step.order || 0) || a.at - b.at || a.cls - b.cls; });
    if (!steps.length) return;
    var timeline = slide.GetTimeLine(), seq = timeline.GetMainSequence(), T = timeline.Timing;
    steps.forEach(function (s) {
      if (FLY[s.cls][s.step.effect] !== undefined) {
        var eff = T.createEffect(s.drawing.Drawing.GetId(), s.cls, 2, FLY[s.cls][s.step.effect], null);   // internal: preset 2 is fly in/out
        eff.cTn.setNodeType(s.step.auto ? AscFormat.NODE_TYPE_AFTEREFFECT : AscFormat.NODE_TYPE_CLICKEFFECT);
        T.addEffectsToMainSequence([eff]);
      } else {
        var effect = seq.AddEffect(s.drawing, PUBLIC[s.step.effect][s.cls - 1], s.step.auto ? "afterprevious" : "onclick");
        // bounce, the nearest preset to a pop, runs 2 s by default
        if (s.step.effect === "pop" && effect) effect.SetDuration(500);
      }
    });
  }
  var TRANSITIONS = { fade: "effectFadeSmoothly", push: "effectPushLeft", magic: "effectMorphByObject" };
  function build(slide, spec) {
    if (spec.background) slide.SetBackground(fillOf(spec.background));
    for (var i = 0; i < spec.shapes.length; i++) {
      var shape = spec.shapes[i], maker = BUILD[shape.kind];
      if (!maker) throw new Error("no builder for a " + shape.kind);
      var drawing = maker(shape);
      if (!slide.AddObject(drawing)) throw new Error("the slide refused shape " + i);
      // Names are internal setName's: the public SetName renames any other holder of the name, on every slide (every
      // slide's heading is "Title"), and so would not give Morph its one "!!" name on two slides. A group's name goes
      // after it joins the slide, or it is lost. A heading that moves keeps Morph's name.
      if (shape.name) drawing.Drawing.setName(shape.name);
      if (shape.magic) drawing.Drawing.setName("!!" + shape.magic);
      if (shape.build) pendingBuilds.push({ slide: slide, drawing: drawing, build: shape.build });
    }
    if (spec.notes) slide.AddNotesText(spec.notes);
    slide.SetVisible(!spec.hidden);
    addBuilds(slide);
    if (spec.transition) {
      var t = Api.CreateSlideShowTransition();
      t.SetEntryEffect(TRANSITIONS[spec.transition.effect]);
      t.SetDuration(spec.transition.ms);
      slide.SetSlideShowTransition(t);
    }
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
    // A new slide shows its layout's background, else its master's: so does this one, unless its HTML sets its own.
    // (A layout leaves a background set on the slide itself in place.)
    if (!slide.FollowLayoutBackground()) slide.FollowMasterBackground();
    // A new slide has no transition: nor has this one, unless its HTML gives one.
    var none = Api.CreateSlideShowTransition();
    none.SetEntryEffect("effectNone");
    slide.SetSlideShowTransition(none);
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
  // A new deck is the blank template's one empty title slide. Slides added to it take its place, as Word writes a new
  // document's first blocks in place of its one empty line. A slide anyone has typed in or changed (its background,
  // transition or comments), or one this call replaces, stays.
  function emptyTemplate(ops) {
    var all = pres.GetAllSlides(), slide = all[0];
    if (all.length !== 1 || !ops.some(function (op) { return op.op === "slides" && op.slides.some(function (s) { return s.id === undefined; }); })) return null;
    if (ops.some(function (op) { return op.op !== "order" && op.slides.some(function (s) { return s.id === idOf(slide); }); })) return null;
    var layout = slide.GetLayout(), transition = slide.GetSlideShowTransition();
    if (!layout || layout.GetName() !== "Title Slide" || !slide.GetVisible() || notesOf(slide) || connectorsOf(slide).length) return null;
    // internal: the builder reads no background; a slide that follows its layout has none of its own
    if (slide.Slide.cSld.Bg || (transition && transition.GetEntryEffect() !== "effectNone") || pres.GetAllComments().length) return null;
    return slide.GetAllDrawings().every(function (d) { return d.GetPlaceholder && d.GetPlaceholder() && !textOf(d); }) ? slide : null;
  }
  function edit(ops) {
    check(ops);
    var empty = emptyTemplate(ops);
    var results = ops.map(function (op, index) {
      if (op.op === "slides") {
        var last = {};
        return { op: index, slides: op.slides.map(function (s) {
          if (s.id !== undefined) return replaced(slideById(s.id), s.spec);
          // Several slides after one place keep their order: each goes after the one added before it.
          var key = String(s.after), slide = added(last[key] !== undefined ? last[key] : s.after, s.spec);
          last[key] = idOf(slide);
          return slide;
        }) };
      }
      if (op.op === "remove") {
        op.slides.forEach(function (s) { slideById(s.id).Delete(); });
        return { op: index };
      }
      op.ids.forEach(function (id, at) { var slide = slideById(id); if (slide.GetSlideIndex() !== at) slide.MoveTo(at); });
      return { op: index, order: pres.GetAllSlides().map(idOf) };
    });
    if (empty) empty.Delete();
    // Numbered once every change is made.
    results.forEach(function (r) {
      if (!r.slides) return;
      r.slides = r.slides.map(function (slide) { return { id: idOf(slide), number: slide.GetSlideIndex() + 1 }; });
      var note = magicNote(ops[r.op].slides.map(function (s) { return s.spec; }), r.slides);
      if (note) r.note = note;
    });
    return { results: results };
  }
  // A slide's transition plays as the slide is entered, so Morph moves what two neighbouring slides both name only when
  // the second arrives by it: ids shared with the slide built before it here, on a slide that does not, move nothing.
  function magicNote(specs, slides) {
    var names = function (spec) { return spec.shapes.map(function (s) { return s.magic; }).filter(Boolean); };
    for (var i = 1; i < specs.length; i++) {
      if (slides[i].number !== slides[i - 1].number + 1 || (specs[i].transition && specs[i].transition.effect === "magic")) continue;
      var before = names(specs[i - 1]);
      if (names(specs[i]).some(function (name) { return before.indexOf(name) >= 0; })) return "ids match across slides but slide " + slides[i].number + " has no data-transition=\"magic\": nothing will move.";
    }
    return null;
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
