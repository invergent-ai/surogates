# Slide decks (.pptx)

You write each slide in the **Claude Slides format**: one `<section>` of HTML on a fixed canvas, every style inline.
runtime-office lays it out exactly as the editor will, then builds it as native PowerPoint shapes, text boxes,
pictures and tables at the measured places: the user gets a slide they can click into and edit, not a picture.

## Reading

- `office_read` view `outline`: the canvas (`[1920, 1080]` on a 16:9 deck; a 4:3 deck is 1920 × 1440) and every slide's
  id, number, title, layout, shape count, whether it has notes or is hidden, and its **hash**.
- view `slide` with `target: <id>`: every shape, in canvas px, with its text.
- view `search` with `query`: the slides that hold the words.

## Editing

- `slides`: add slides from HTML (`after` a slide id, `"start"`, or last), or replace one in place with its `id` and
  the `hash` the outline gave. A slide the user changed since you read it refuses the call: read it again.
- `order`: every slide's id, in the order wanted (a call of its own).
- `remove`: slides by `id` and `hash`.

At most ten slides per call; one or two per call lets the user watch the deck grow.

```json
{"op": "slides", "slides": [{"after": "start", "html": "<section style=\"background:#14213D;color:#FBFBF8;font-family:Georgia, serif;padding:128px;display:flex;flex-direction:column;justify-content:center;gap:32px\"><h1 style=\"font-size:120px;font-weight:600;line-height:1.1\">Lisbon office</h1><p style=\"font-size:44px;color:#BFD8D5\">Why, when and what it costs</p><aside>Open with the decision we need today.</aside></section>"}]}
```

```json
{"op": "slides", "slides": [{"html": "<section style=\"background:#FBFBF8;color:#1A1A1A;font-family:Arial, sans-serif;padding:128px;display:flex;flex-direction:column;gap:48px\"><h2 style=\"font-size:72px;font-weight:600;line-height:1.1\">Two numbers</h2><div style=\"display:flex;flex-direction:row;gap:32px\"><div style=\"flex:1;background:#FFFFFF;border:2px solid #E3E6E4;padding:40px;display:flex;flex-direction:column;gap:12px\"><p style=\"font-size:96px;font-weight:600;color:#2F5597\">12</p><p style=\"font-size:32px\">engineers by June</p></div><div style=\"flex:1;background:#FFFFFF;border:2px solid #E3E6E4;padding:40px;display:flex;flex-direction:column;gap:12px\"><p style=\"font-size:96px;font-weight:600;color:#2F5597\">€1.2M</p><p style=\"font-size:32px\">first-year cost</p></div></div><p style=\"position:absolute;left:128px;bottom:64px;width:1664px;font-size:24px;color:#6A7179\">Source: finance plan, March</p></section>"}]}
```

```json
{"op": "remove", "slides": [{"id": "1_402", "hash": "ab12cd34"}]}
```

```json
{"op": "order", "ids": ["1428", "1_630", "1473"]}
```

## The format

**The slide** is `<section style="…">` with, always, a `background`; the text defaults (`font-family`, `color`); and its
layout: `display:flex; flex-direction:column` (the default) or `display:grid`, `padding:128px` (the margins: 1664 × 824
inside on 16:9), `gap`, `align-items`, `justify-content`. Children flow in it. `position:absolute` pins a child to the
slide (`left`, `top`, `right`, `bottom`, `width`, `height`; give pinned text a `width`). Flow content paints as one
layer where its first flow child sits: a pinned backdrop listed before it paints below, anything pinned after it above.
In a `position:relative` div, pinned children paint over its flow. `hidden` skips the slide; `data-transition="fade|
push|magic"` is how it leaves.

**Text:** `<h1>` `<h2>` `<h3>` `<p>` (start at 96/64/44/32 px; set `font-size` yourself, **never under 24 px**),
`<ul>`/`<ol>` of plain `<li>` (one level), `<br>`, and inline `<b>` `<i>` `<u>` `<a href="https://…">` `<span
style="color:…">` (no size, font, background or border on them: a badge is a `<div>` of its own). A `<div>` holding only
text and those inline tags is one text box of one paragraph, set by its own font, colour, alignment, background, border
and padding; pieces its own flex or grid layout sets apart go in a `<p>` each. `<div data-text>` of plain
`<p>`/`<ul>`/`<ol>` is one text box of several paragraphs. Text beside blocks in one div, or directly in the section, is
refused: put it in a `<p>`. Inside a heading, paragraph, list item, table cell or div of text go only `<b>` `<i>` `<u>`
`<a>` `<span>` `<br>`: no icon, picture or other block. A label is a `<p>` with `background` and `padding`: it stays one
shape. Text wraps only at spaces, so size a box to its longest word (about 0.6 × the font size per character). **Too
much for its box? The slide is refused: split it. Nothing shrinks.**

**Containers:** `<div>` is a flex column unless it says `display:flex` (a row) or `display:grid`; it is invisible unless
painted (`background`, `border`, `box-shadow`). An empty `<div style="flex:1">` is a spacer. At most 15 nested divs.

**Pictures:** `<img src="<workspace path>" alt="…" style="width; height; object-fit:cover|contain">`, a PNG, JPEG or
SVG from the workspace. `data-crop="ZOOM X% Y%"` crops. `<svg aria-label>` (at most 52 KB) is drawn as a picture. An
SVG, from a file or inline, is plain shapes: no script, foreignObject or `on…` attribute, no `<!ENTITY>` or other DTD
declaration, and no link but to `#…` or to a PNG or JPEG as data. matplotlib's SVGs go in as they are.

**Tables:** `<table>` of `<tr>` with `<th>` (the first row) and `<td>`, plain text, no colspan or rowspan. Column widths:
`width:N%` on every cell of the first row. A `<tr>` may carry `background`. A row is about 2.1 × the font size per line.

**Shapes:** a painted `<div>` is a rectangle; `<hr>` is a line; `<x-shape kind="rect|ellipse|diamond|arrow-right|
arrow-left|arrow-up|arrow-down|line">` fills its box. `<x-connector x1 y1 x2 y2 route="straight|hv|vh|elbow"
head-end="open|filled">` draws a line between points. `<x-icon name="…" style="color; width; height">`, name one of
Activity Book Chart Chat Check CheckCircle Clock Cloud Code Database Globe GraduationCap Home Key Lightbulb Lightning
Link Lock PaperPlane Play Search Settings Star ThumbsUp Tool Trust Users Verified Warning Wrench. Nothing goes inside an
`<x-shape>`, `<x-icon>`, `<x-connector>` or `<hr>`: a label goes in a text element beside it. `<x-embed>` (a static HTML
and CSS snippet, pinned as a direct child of the section, at most 16 KB and 8 per slide) goes in as a picture of what it
shows. Scripts in it do not run: draw a chart as an inline `<svg>`.

**Styles:** lengths in px (no `em`, `rem`, `vw`, `%` only where a box's share is meant); colours as hex or `rgb[a]()`;
`linear-gradient` and `repeating-linear-gradient` backgrounds; `border` (solid, dashed, dotted, double); `box-shadow`;
`opacity`; `transform: rotate()`, `translate()`, `scale()`; `letter-spacing`, `text-transform`, `text-decoration`,
`-webkit-text-stroke`, gradient text (`background-clip:text`); `margin` (not negative), which moves a box as CSS does.
No `z-index`, no classes or `<style>`. A radial gradient is a picture behind its box's text. A filter or a blend mode
on anything, opacity on a div that holds children, and a turn, a scale or a skew on such a div or on a table make the
element and everything in it one picture, which looks right but which the user cannot edit: keep them for decoration,
and fade or turn single shapes and text boxes instead (a turned text box stays native; a scaled or skewed one is a
picture). A blurred shadow is a picture behind its shape, which stays editable.

**Motion:** `data-build-in` / `data-build-out="fade|rise|drop|left|right|scale|pop [order N] [auto]"` on the section's
pinned children; `data-transition="magic"` with the same `id` on matching pinned elements of two slides moves them
between the slides. A build or a magic-move `id` inside another moving element is refused, and so is a table inside a
moving box: give the table its own motion. A connector or a build inside a box that goes in as one picture (see Styles)
is refused too.

**Notes:** one `<aside>` of plain text, the section's last child.

**Fonts:** the document server's own, so the text sets exactly as measured. A `font-family` list resolves to its first
font the server has, then the generic: Arial, Calibri, Cambria, Georgia, Montserrat, Noto Sans, Open Sans, Times New
Roman, Verdana, Trebuchet MS and Courier New are there; `sans-serif` is Arial, `serif` Times New Roman. At most 200
elements per slide.

## Designing the deck

- **One idea per slide.** A statement beats bullets; turn lists into tables, card rows, big numbers or quotes.
- **A type scale of four or five sizes** (for example 120 / 72 / 44 / 32 / 24) and one or two fonts; emphasise with
  weight, italic or colour, not a new size.
- **A hex palette, chosen once:** one dark, one light, one or two accents; toned whites and blacks (#FBFBF8, #14213D)
  rather than pure. Text holds 4.5:1 contrast on its background (3:1 from 44 px).
- **The vertical space adds up:** 824 px inside the margins on 16:9. A heading is about size × lines × 1.1; a card is
  lines × size × line height plus its padding. Leave a card's height out and let it fit its text.
- **Too much? Split the slide.** Nothing shrinks.
- **Square corners.** Never round a card, label, frame or picture: no `border-radius`, no `kind="rounded"`.
- Keep repeated elements in the same place on every slide of a kind, the heading at the top margin.
