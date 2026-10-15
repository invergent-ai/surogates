import type { Build, Canvas, Color, Fill, Geometry, Para, Run, Shadow, ShapeSpec, SlideSpec, Stroke } from "./measure.js";

export interface InspectArgs { phase: "check" | "faces" | "shapes"; canvas: Canvas; minPx: number; icons: string[] }
/**
 * What a picture Chromium takes of an element shows: the element alone ("element"); its box with its text hidden
 * ("box", its children hidden too; "text", its text's shadows kept); its box-shadow alone ("shadow"); or everything
 * painted where it is ("region", for a blend mode or a backdrop filter, which show what lies under the element).
 */
export type Shot = "element" | "box" | "text" | "shadow" | "region";
export type Inspected =
  | { error: string }
  | { stacks: string[]; faces: { family: string; bold: boolean; italic: boolean }[] }
  | { spec: SlideSpec; icons: { name: string; color: string; px: number }[]; embeds: { html: string; w: number; h: number }[]; shots: Shot[] };

// Runs in the measuring page. "check" holds the slide to Claude Slides' format and says which font stacks and faces
// its text uses; "faces" only says which; "shapes" measures it into a SlideSpec. An svg, an icon, an embed or what only
// Chromium can draw becomes a placeholder picture ("svg:n", "icon:n", "embed:n", "shot:n") that Node draws afterwards.
export function inspect(args: InspectArgs): Inspected {
  const TAGS = new Set(["SECTION", "H1", "H2", "H3", "P", "UL", "OL", "LI", "BR", "B", "I", "U", "A", "SPAN", "DIV", "IMG", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "SVG", "HR", "X-SHAPE", "X-ICON", "X-CONNECTOR", "X-EMBED", "ASIDE"]);
  const TEXT = new Set(["H1", "H2", "H3", "P"]);
  const INLINE = new Set(["B", "I", "U", "A", "SPAN", "BR"]);
  const ATTRIBUTES: Record<string, string[]> = {
    SECTION: ["data-section", "data-transition", "hidden"], DIV: ["data-text", "data-fit"], IMG: ["src", "alt", "data-crop"], A: ["href"],
    "X-SHAPE": ["kind"], "X-ICON": ["name"], "X-CONNECTOR": ["x1", "y1", "x2", "y2", "head", "head-start", "head-end", "route", "from"],
  };
  const SHAPES = ["rect", "rounded", "ellipse", "diamond", "arrow-right", "arrow-left", "arrow-up", "arrow-down", "line"];
  const CHOICES: Record<string, string[]> = {
    "data-transition": ["fade", "push", "magic"], route: ["straight", "hv", "vh", "elbow"], head: ["none", "start", "end", "both"],
    "head-start": ["none", "open", "filled"], "head-end": ["none", "open", "filled"], from: ["tl", "tr", "bl", "br"],
  };
  const BUILDS = /^(fade|rise|drop|left|right|scale|pop)(\s+order\s+\d+)?(\s+auto)?$/;
  // Elements that draw themselves and nothing inside them.
  const EMPTY = new Set(["X-SHAPE", "X-ICON", "X-CONNECTOR", "HR"]);
  // Claude Slides' style properties, as the longhands Chromium expands them into.
  const STYLE = /^(position|left|top|right|bottom|width|height|min-width|min-height|max-width|max-height|display|flex-direction|flex-wrap|row-gap|column-gap|align-items|justify-content|justify-items|align-self|justify-self|flex-grow|flex-shrink|flex-basis|grid-template-columns|grid-template-rows|grid-template-areas|grid-column-start|grid-column-end|grid-row-start|grid-row-end|aspect-ratio|padding-(top|right|bottom|left)|overflow-x|overflow-y|font-family|font-size|font-weight|font-style|font-variant-.+|font-stretch|font-size-adjust|font-kerning|font-optical-sizing|font-feature-settings|font-variation-settings|line-height|letter-spacing|text-align|text-transform|white-space|white-space-collapse|text-wrap|text-wrap-mode|text-wrap-style|text-decoration-.+|-webkit-text-stroke-(width|color)|color|background-.+|-webkit-text-fill-color|border-(top|right|bottom|left)-(width|style|color)|border-(top-left|top-right|bottom-right|bottom-left)-radius|border-image-.+|box-shadow|text-shadow|opacity|transform|filter|backdrop-filter|mix-blend-mode|object-fit|margin-(top|right|bottom|left)|box-sizing)$/;
  const UNITS = /\d(?:\.\d+)?(?:rem|vw|vh|vmin|vmax|ch|ex|cm|mm|in|pc|q)\b/i;
  const { canvas } = args;
  const ptPerPx = canvas.emuPerPx / 12700;

  class Refused extends Error {}
  const fail = (message: string): never => {
    throw new Refused(message);
  };
  const tagOf = (el: Element): string => el.tagName.toUpperCase();
  const describe = (el: Element): string => {
    const parts: string[] = [];
    for (let at: Element | null = el; at && at !== document.body; at = at.parentElement) {
      const parent: Element | null = at.parentElement;
      const tag = at.tagName;
      const same = parent ? Array.from(parent.children).filter((child) => child.tagName === tag) : [];
      parts.unshift(at.tagName.toLowerCase() + (same.length > 1 ? `:nth-of-type(${same.indexOf(at) + 1})` : ""));
    }
    const words = (el.textContent ?? "").trim().split(/\s+/).slice(0, 6).join(" ");
    return `<${parts.join(" > ")}>${words ? ` ("${words}")` : ""}`;
  };
  const round = (value: number): number => Math.round(value * 100) / 100;
  const emu = (px: number): number => Math.round(px * canvas.emuPerPx);
  const twips = (px: number): number => Math.round(px * ptPerPx * 20);
  const rgba = (css: string): Color | null => {
    const m = /^rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(css.trim());
    if (!m) return null;
    return { hex: `#${[m[1], m[2], m[3]].map((v) => Math.round(Number(v)).toString(16).padStart(2, "0")).join("")}`, alpha: m[4] === undefined ? 1 : Number(m[4]) };
  };
  // A colour that shows: none for a transparent one.
  const color = (css: string): Color | null => {
    const c = rgba(css);
    return c && c.alpha > 0 ? c : null;
  };
  const family = (cs: CSSStyleDeclaration): string => cs.fontFamily.split(",")[0]!.trim().replace(/^["']|["']$/g, "");
  // Inside an svg or an embed is no slide content: the one is drawn whole, the other runs on a page of its own.
  const inside = (el: Element): boolean => !!el.parentElement?.closest("svg, x-embed");
  const pinned = (el: Element): boolean => getComputedStyle(el).position === "absolute";
  const isHost = (el: Element): boolean => tagOf(el) === "SECTION" || ["relative", "absolute"].includes(getComputedStyle(el).position);
  // A div holding only text and inline elements is one text box of one paragraph, as a p is.
  const textOnly = (el: Element): boolean => tagOf(el) === "DIV" && !!(el.textContent ?? "").trim()
    && Array.from(el.childNodes).every((n) => n.nodeType !== Node.ELEMENT_NODE || INLINE.has(tagOf(n as Element)));
  // A box an inline element paints, which a run of text cannot carry: a background (not one clipped to the text), a
  // border or a shadow.
  const paintsBox = (el: Element): boolean => {
    const cs = getComputedStyle(el);
    return (cs.backgroundClip !== "text" && (!!color(cs.backgroundColor) || cs.backgroundImage !== "none")) || cs.boxShadow !== "none"
      || (["Top", "Right", "Bottom", "Left"] as const).some((side) => cs[`border${side}Style`] !== "none" && parseFloat(cs[`border${side}Width`]) > 0);
  };
  const isText = (el: Element): boolean => TEXT.has(tagOf(el)) || ["UL", "OL"].includes(tagOf(el)) || (tagOf(el) === "DIV" && (el.hasAttribute("data-text") || textOnly(el)));

  // The document server keeps a slide's SVG picture and draws it itself, so it holds shapes, never code or a reference
  // to anything outside it. Each is read as this page parsed it: an img's src as the browser decoded it, an svg's tree.
  const PLAIN = "A slide's SVG is plain shapes: no script, foreignObject or on… attribute, no <!ENTITY> or other DTD declaration, and no link but to #… or to a PNG or JPEG as data.";
  // A data URL's bytes, as the browser reads them: percent-decoded, then base64-decoded when it says so.
  const dataBytes = (url: string): Uint8Array | null => {
    const comma = url.indexOf(",");
    const body = url.slice(comma + 1);
    if (/;\s*base64\s*$/i.test(url.slice(0, comma))) {
      let binary: string;
      try {
        binary = atob(body.replace(/%([\da-f]{2})/gi, (_, hex: string) => String.fromCharCode(parseInt(hex, 16))));
      } catch {
        return null;
      }
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return bytes;
    }
    const raw = new TextEncoder().encode(body), bytes: number[] = [];
    for (let i = 0; i < raw.length; i++) {
      const hex = raw[i] === 0x25 ? String.fromCharCode(raw[i + 1] ?? 0, raw[i + 2] ?? 0) : "";
      if (/^[\da-f]{2}$/i.test(hex)) {
        bytes.push(parseInt(hex, 16));
        i += 2;
      } else bytes.push(raw[i]!);
    }
    return Uint8Array.from(bytes);
  };
  // The document server tells a picture's kind by its bytes, whatever its data URL says: a PNG's whole signature, or
  // a JPEG's.
  const SIGNATURES = [[0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], [0xff, 0xd8, 0xff]];
  const raster = (b: Uint8Array | null): boolean => !!b && SIGNATURES.some((signature) => signature.every((byte, i) => b[i] === byte));
  // The picture's name, built only for a refusal: building it walks the page.
  type Who = () => string;
  const reference = (target: string, who: Who): void => {
    const to = target.trim();
    if (to.startsWith("#") || (/^data:image\/(png|jpeg)[;,]/i.test(to) && raster(dataBytes(to)))) return;
    fail(`${who()}: the SVG links to ${to.slice(0, 80)}. ${PLAIN}`);
  };
  // url() and @import, CSS escapes read as CSS reads them.
  const styleOf = (css: string, who: Who): void => {
    const plain = css.replace(/\\(?:([\da-f]{1,6})\s?|([^]))/gi, (_, hex: string | undefined, char: string | undefined) => (hex ? String.fromCodePoint(Math.min(parseInt(hex, 16), 0x10ffff)) : char!));
    if (/@import/i.test(plain)) fail(`${who()}: the SVG's style imports a sheet. ${PLAIN}`);
    for (const m of plain.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))/gi)) reference(m[1] ?? m[2] ?? m[3]!, who);
  };
  const svgTree = (root: Element, who: Who): void => {
    for (const node of [root, ...Array.from(root.querySelectorAll("*"))]) {
      const name = node.localName.toLowerCase();
      if (name === "script" || name === "foreignobject") fail(`${who()}: the SVG holds a ${node.localName}. ${PLAIN}`);
      if (name === "style") styleOf(node.textContent ?? "", who);
      for (const attribute of Array.from(node.attributes)) {
        const at = attribute.localName.toLowerCase();
        if (at.startsWith("on")) fail(`${who()}: the SVG has the attribute ${attribute.name}. ${PLAIN}`);
        if (at === "href" || attribute.name === "xml:base") reference(attribute.value, who);
        if (at === "attributename" && /^\s*([\w.-]+:)?(href|on)/i.test(attribute.value)) fail(`${who()}: the SVG animates ${attribute.value.trim()}. ${PLAIN}`);
        styleOf(attribute.value, who);
      }
    }
  };
  // An img's SVG, checked, as the document server is handed it: the tree this page parsed, as UTF-8, without its
  // DOCTYPE. A DOCTYPE that only names a DTD (matplotlib writes one) is allowed; nothing ever reads that DTD.
  const svgPicture = (src: string, who: string): string => {
    const bytes = dataBytes(src) ?? fail(`${who}: the SVG's data is not base64.`);
    const le = bytes[0] === 0xff && bytes[1] === 0xfe, be = bytes[0] === 0xfe && bytes[1] === 0xff;
    const text = new TextDecoder(le ? "utf-16le" : be ? "utf-16be" : "utf-8").decode(bytes);
    // A DOCTYPE's internal subset does nothing without one of these; a scan for them is linear, as parsing a DOCTYPE
    // by regex is not.
    const declared = /<!(ENTITY|ATTLIST|ELEMENT|NOTATION)/i.exec(text);
    if (declared) fail(`${who}: the SVG declares <!${declared[1]!.toUpperCase()}>. ${PLAIN}`);
    const doc = new DOMParser().parseFromString(text, "image/svg+xml");
    if (doc.getElementsByTagName("parsererror").length || doc.documentElement?.localName !== "svg") fail(`${who}: the SVG does not parse. ${PLAIN}`);
    const instruction = doc.createTreeWalker(doc, NodeFilter.SHOW_PROCESSING_INSTRUCTION).nextNode() as ProcessingInstruction | null;
    if (instruction) fail(`${who}: the SVG holds <?${instruction.target} …?>. ${PLAIN}`);
    svgTree(doc.documentElement, () => who);
    // The root alone: no XML declaration naming another encoding, no DOCTYPE.
    const utf8 = new TextEncoder().encode(new XMLSerializer().serializeToString(doc.documentElement));
    let binary = "";
    for (let i = 0; i < utf8.length; i += 0x8000) binary += String.fromCharCode(...utf8.subarray(i, i + 0x8000));
    return `data:image/svg+xml;base64,${btoa(binary)}`;
  };

  try {
    const root = document.body.firstElementChild as HTMLElement | null;
    const bodyText = Array.from(document.body.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim());
    if (!root || tagOf(root) !== "SECTION" || document.body.children.length !== 1 || bodyText) fail("A slide is one <section> and nothing beside it.");
    const section = root!;
    const elements = [section, ...Array.from(section.querySelectorAll("*"))].filter((el) => !inside(el));

    // --- the text's font stacks and faces, and its size
    const fontsUsed = (): Inspected => {
      const stacks = new Set<string>();
      const faces = new Map<string, { family: string; bold: boolean; italic: boolean }>();
      const walker = document.createTreeWalker(section, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const parent = node.parentElement!;
        if (!(node.textContent ?? "").trim() || inside(parent) || parent.closest("aside")) continue;
        const cs = getComputedStyle(parent);
        if (parseFloat(cs.fontSize) < args.minPx - 0.01) fail(`Text in ${describe(parent)} is ${round(parseFloat(cs.fontSize))} px; the least on a slide is ${args.minPx} px.`);
        stacks.add(cs.fontFamily);
        const face = { family: family(cs), bold: parseInt(cs.fontWeight, 10) >= 600, italic: cs.fontStyle === "italic" };
        faces.set(`${face.family}|${face.bold}|${face.italic}`, face);
      }
      return { stacks: [...stacks], faces: [...faces.values()] };
    };
    if (args.phase === "faces") return fontsUsed();

    // --- the format: tags, attributes, styles and limits (Claude Slides' format.md)
    // Checked once: the measurer then writes each element's resolved family inline, which the format refuses on a span.
    if (args.phase === "check") {
      if (elements.length > 200) fail("A slide has at most 200 elements: split it.");
      // An svg inside another is walked with it.
      for (const el of Array.from(section.querySelectorAll("svg"))) if (!el.parentElement?.closest("svg")) svgTree(el, () => describe(el));
      let embeds = 0;
      for (const el of elements) {
        const tag = tagOf(el);
        if (!TAGS.has(tag)) fail(`${describe(el)}: a slide holds h1-h3, p, ul, ol, li, br, b, i, u, a, span, div, img, table, tr, th, td, svg, hr, x-shape, x-icon, x-connector, x-embed and one aside, not <${tag.toLowerCase()}>.`);
        if (tag !== "SVG") {
          for (const attribute of Array.from(el.attributes)) {
            const name = attribute.name;
            const own = ATTRIBUTES[tag] ?? [];
            // The format ignores class, and data-* it does not name.
            const value = attribute.value.trim();
            if (own.includes(name) && CHOICES[name] && !CHOICES[name].includes(value)) fail(`${describe(el)}: ${name} is one of ${CHOICES[name].join(", ")}.`);
            if (/^data-build-(in|out)$/.test(name) && !BUILDS.test(value)) fail(`${describe(el)}: ${name} is "fade|rise|drop|left|right|scale|pop [order N] [auto]".`);
            if (/^data-build-(in|out)$/.test(name) && !pinned(el)) fail(`${describe(el)}: builds go on pinned elements (position:absolute).`);
            // A transition is a slide's: the editor plays it as the slide is entered.
            if (name === "data-transition" && tag !== "SECTION") fail(`${describe(el)}: data-transition belongs on the <section> of the slide it leads into.`);
            if (tag === "IMG" && name === "data-crop" && !(/^\d+(\.\d+)?(\s+-?\d+(\.\d+)?%){0,2}$/.test(value) && parseFloat(value) >= 1)) fail(`${describe(el)}: data-crop is "ZOOM X% Y%", ZOOM at least 1.`);
            if (["style", "id", "class", "data-build-in", "data-build-out"].includes(name) || own.includes(name) || name.startsWith("data-")) continue;
            fail(`${describe(el)} has the attribute ${name}, which the slide format does not take there.`);
          }
        }
        if (tag === "ASIDE" && (el.parentElement !== section || el !== section.lastElementChild)) fail("The <aside> of speaker notes is the section's last child.");
        if (tag === "A" && !/^(https?:|mailto:)/i.test(el.getAttribute("href") ?? "")) fail(`${describe(el)}: a link goes to an http(s) or mailto address.`);
        if (tag === "IMG") {
          const src = el.getAttribute("src") ?? "";
          // Drawn and handed on as checked.
          if (/^data:image\/svg\+xml[;,]/i.test(src)) el.setAttribute("src", svgPicture(src, describe(el)));
          else if (!/^data:image\/(png|jpeg);base64,/i.test(src)) fail(`${describe(el)}: a picture from the workspace (a PNG, JPEG or SVG).`);
          else if (!raster(dataBytes(src))) fail(`${describe(el)}: its data is not a PNG or JPEG.`);
        }
        if (tag === "X-SHAPE" && !SHAPES.includes(el.getAttribute("kind") ?? "")) fail(`${describe(el)}: kind is one of ${SHAPES.join(", ")}.`);
        if (EMPTY.has(tag) && (el.children.length || (el.textContent ?? "").trim())) fail(`${describe(el)}: a <${el.tagName.toLowerCase()}> draws nothing inside it; text goes in a text element beside it.`);
        if (tag === "X-CONNECTOR") {
          const given = ["x1", "y1", "x2", "y2"].filter((name) => el.hasAttribute(name));
          if (given.length % 4 || given.some((name) => !/^-?\d+(\.\d+)?(px|%)?$/.test(el.getAttribute(name)!.trim()))) fail(`${describe(el)}: an x-connector gives x1, y1, x2 and y2 (px, or % of its host's box), or none of them.`);
        }
        if (tag === "X-ICON" && !args.icons.includes(el.getAttribute("name") ?? "")) fail(`${describe(el)}: name is one of ${args.icons.join(", ")}.`);
        if (tag === "X-EMBED") {
          if (el.parentElement !== section || !pinned(el)) fail(`${describe(el)}: an x-embed is pinned (position:absolute) and a child of the section.`);
          if (el.innerHTML.length > 16_384) fail(`${describe(el)}: an x-embed is at most 16 KB.`);
          if (++embeds > 8) fail("A slide has at most 8 x-embeds.");
        }
        if (tag === "SVG" && el.outerHTML.length > 53_248) fail(`${describe(el)}: an svg is at most 52 KB.`);
        if (tag === "UL" || tag === "OL") {
          for (const li of Array.from(el.children)) {
            if (tagOf(li) !== "LI" || Array.from(li.children).some((c) => !INLINE.has(tagOf(c)))) fail(`${describe(li)}: a list holds plain <li> items, one level.`);
          }
        }
        // Only text elements lay text out: text anywhere else would not be drawn. A div of text alone is one.
        if ((tag === "SECTION" || tag === "DIV" || tag === "UL" || tag === "OL") && !textOnly(el)) {
          const blocks = Array.from(el.children).some((child) => !INLINE.has(tagOf(child)));
          for (const node of Array.from(el.childNodes)) {
            const child = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : null;
            if (child ? !INLINE.has(tagOf(child)) : node.nodeType !== Node.TEXT_NODE || !(node.textContent ?? "").trim()) continue;
            const words = (node.textContent ?? "").trim().split(/\s+/).slice(0, 6).join(" ");
            const what = child ? describe(child) : `Text "${words}"`;
            if (tag === "UL" || tag === "OL") fail(`${what} sits directly in ${describe(el)}: put it in an <li>.`);
            // In a p, an inline element with no text would be an empty text box, and the box it paints would be lost.
            const fix = child && !(child.textContent ?? "").trim() ? "draw it as a <div>" : "put it in a <p>";
            fail(`${what} sits ${blocks ? "beside blocks in" : "directly in"} ${describe(el)}: ${fix}.`);
          }
        }
        if (tag === "DIV" && el.hasAttribute("data-text") && !textOnly(el)) {
          for (const child of Array.from(el.children)) if (!["P", "UL", "OL"].includes(tagOf(child))) fail(`${describe(child)}: a div[data-text] holds p, ul and ol, or only text.`);
        }
        if (TEXT.has(tag) || tag === "LI" || tag === "TD" || tag === "TH" || textOnly(el)) {
          const holder = tag === "TD" || tag === "TH" ? "a table cell holds plain text" : tag === "DIV" ? "a div of text holds text" : "a heading, paragraph or list item holds text";
          for (const child of Array.from(el.querySelectorAll("*"))) {
            if (!INLINE.has(tagOf(child))) fail(`${describe(child)}: ${holder} (b, i, u, a, span and br), not <${child.tagName.toLowerCase()}>.`);
            if (paintsBox(child)) fail(`${describe(child)}: a painted <${child.tagName.toLowerCase()}> inside text can't be drawn: draw it as a <div> beside the text.`);
          }
        }
        // One text box sets a div's pieces in one line of runs: a flex or grid layout of its own that keeps them apart
        // would glue their words together.
        if (textOnly(el) && /flex|grid/.test((el as HTMLElement).style.display)) {
          let pieces = 0, inText = false;
          for (const node of Array.from(el.childNodes)) {
            if (node.nodeType === Node.ELEMENT_NODE) {
              pieces++;
              inText = false;
            } else if (node.nodeType === Node.TEXT_NODE && (node.textContent ?? "").trim() && !inText) {
              pieces++;
              inText = true;
            }
          }
          const cs = getComputedStyle(el);
          // A grid puts each piece in a cell of its own, across or down.
          const apart = /grid/.test(cs.display) || cs.flexDirection !== "row" || /space/.test(cs.justifyContent) || parseFloat(cs.rowGap) > 0 || parseFloat(cs.columnGap) > 0;
          if (pieces > 1 && apart) fail(`${describe(el)} lays out its pieces apart (gap, space-between or columns), which one text box cannot: put each in its own <p>.`);
        }
        if (tag === "TR" && Array.from((el as HTMLTableRowElement).cells).some((c) => c.colSpan > 1 || c.rowSpan > 1)) fail(`${describe(el)}: no colspan or rowspan.`);
        // A cell's shading is one colour without alpha: the editor draws a translucent one opaque, over the text's own
        // colour as often as not, and a gradient or picture would be lost.
        if (["TR", "TD", "TH"].includes(tag)) {
          const cs = getComputedStyle(el), translucent = (color(cs.backgroundColor)?.alpha ?? 1) < 1;
          if (translucent || cs.backgroundImage !== "none") {
            fail(`${describe(el)}: a table ${tag === "TR" ? "row" : "cell"}'s background must be a solid colour (the editor draws ${translucent ? "it opaque" : "no gradient or picture there"}).`);
          }
        }
        const style = (el as HTMLElement).style;
        for (let i = 0; i < (style?.length ?? 0); i++) {
          const property = style[i]!;
          const value = style.getPropertyValue(property);
          // A shorthand sets its other longhands to their initial value.
          if (value === "initial") continue;
          if (!STYLE.test(property)) fail(`${describe(el)} uses ${property}, which is not in the slide format.`);
          // Pulled back, a box lets Chromium fit text where the editor's runs and paragraphs need more room.
          if (/^margin-/.test(property) && parseFloat(getComputedStyle(el).getPropertyValue(property)) < 0) fail(`${describe(el)}: no negative margin: use gap or padding.`);
          if (/var\(|currentcolor/i.test(value)) fail(`${describe(el)}: no var() or currentcolor.`);
          if (UNITS.test(value) || (property !== "letter-spacing" && /\d(?:\.\d+)?em\b/i.test(value))) fail(`${describe(el)} sets ${property} to ${value}: lengths are px (no em, rem, vw or vh).`);
          if (tag === "SPAN" && /^font-(size|family)$/.test(property)) fail(`${describe(el)}: no sizes or fonts on a span (use two blocks).`);
        }
        let depth = 0;
        for (let at = el.parentElement; at && at !== section; at = at.parentElement) if (tagOf(at) === "DIV") depth++;
        if (tag === "DIV" && depth >= 15) fail(`${describe(el)}: at most 15 nested divs.`);
      }
      if ((section.textContent ?? "").length > 20_000) fail("A slide holds at most 20,000 characters of text.");
      // A div of text sets its text in lines, as a p does: as a flex or grid box it would make each inline element a
      // block of its own. Where that box put its text, the text box aligns it.
      for (const el of elements as HTMLElement[]) {
        el.removeAttribute("data-anchor");
        const cs = getComputedStyle(el);
        if (!textOnly(el) || !/flex|grid/.test(cs.display)) continue;
        // A flex box places its line by justify-content and align-items; a grid box also by justify-items and align-content.
        const flex = /flex/.test(cs.display), column = flex && cs.flexDirection.startsWith("column");
        const main = flex ? cs.justifyContent : `${cs.justifyContent} ${cs.justifyItems}`, cross = flex ? cs.alignItems : `${cs.alignItems} ${cs.alignContent}`;
        const side = (value: string) => (/center|around|evenly/.test(value) ? "center" : /end|right/.test(value) ? "end" : "start");
        const across = side(column ? cross : main), down = side(column ? main : cross);
        if (across !== "start") el.style.textAlign = across === "center" ? "center" : "right";
        if (down !== "start") el.setAttribute("data-anchor", down === "center" ? "ctr" : "b");
        el.style.display = cs.display.startsWith("inline") ? "inline-block" : "block";
      }
      return fontsUsed();
    }

    // --- with the resolved fonts loaded: nothing past the slide's edge, nothing overflowing its box
    const origin = section.getBoundingClientRect();
    const shown = elements.filter((el) => tagOf(el) !== "ASIDE" && getComputedStyle(el).display !== "none");
    // The edges first: a box past them also makes the section overflow, which says less.
    for (const el of shown) {
      // An inline box is as tall as its glyphs, not its lines; the text element holding it is checked.
      if (el === section || INLINE.has(tagOf(el))) continue;
      const r = el.getBoundingClientRect();
      const past = Math.max(origin.left - r.left, r.right - origin.right, origin.top - r.top, r.bottom - origin.bottom);
      if (past > 0.5) fail(`${describe(el)} reaches ${Math.ceil(past)} px past the slide's edge: move it in, or split the slide.`);
    }
    // The editor lays a text box's lines by their pitch, but Chromium's scroll overflow also counts the glyphs'
    // content area reaching past a line height tighter than it: that much is no overflow. An inline box is as tall as
    // its font's content area whatever the line height, so a hidden span in the element's font measures it.
    const areas = new Map<string, number>();
    const overhang = (el: Element): number => {
      const cs = getComputedStyle(el);
      const lineHeight = parseFloat(cs.lineHeight);
      if (!Number.isFinite(lineHeight)) return 0;
      const key = `${cs.fontStyle}|${cs.fontWeight}|${cs.fontSize}|${cs.fontFamily}`;
      let area = areas.get(key);
      if (area === undefined) {
        const holder = document.createElement("div");
        holder.style.cssText = "position:absolute;left:0;top:0;display:block;visibility:hidden";
        const probe = holder.appendChild(document.createElement("span"));
        Object.assign(probe.style, { fontFamily: cs.fontFamily, fontSize: cs.fontSize, fontWeight: cs.fontWeight, fontStyle: cs.fontStyle });
        probe.textContent = "Hg";
        section.appendChild(holder);
        area = probe.getBoundingClientRect().height;
        holder.remove();
        areas.set(key, area);
      }
      return Math.max(0, area - lineHeight);
    };
    for (const el of shown) {
      const tag = tagOf(el);
      const cs = getComputedStyle(el);
      let over: number;
      if ((tag === "DIV" && !textOnly(el)) || tag === "UL" || tag === "OL") {
        // A clipping div clips what spills; the text boxes inside it are checked themselves. The editor clips no text box.
        if (tag === "DIV" && !el.hasAttribute("data-text") && cs.overflowX === "hidden") continue;
        // Its children's border boxes, laid by line height, stay in its content box; a pinned child it places, in its
        // padding box. A text box's paragraphs and a list's items are blocks like any other child.
        const r = el.getBoundingClientRect();
        const padding = { left: r.left + parseFloat(cs.borderLeftWidth), right: r.right - parseFloat(cs.borderRightWidth), top: r.top + parseFloat(cs.borderTopWidth), bottom: r.bottom - parseFloat(cs.borderBottomWidth) };
        const content = { left: padding.left + parseFloat(cs.paddingLeft), right: padding.right - parseFloat(cs.paddingRight), top: padding.top + parseFloat(cs.paddingTop), bottom: padding.bottom - parseFloat(cs.paddingBottom) };
        over = 0;
        for (const child of Array.from(el.children)) {
          if (getComputedStyle(child).display === "none") continue;
          if (pinned(child) && !isHost(el)) continue;
          const inner = pinned(child) ? padding : content;
          const c = child.getBoundingClientRect();
          over = Math.max(over, inner.left - c.left, c.right - inner.right, inner.top - c.top, c.bottom - inner.bottom);
        }
      } else if (TEXT.has(tag) || tag === "LI" || tag === "TD" || tag === "TH" || textOnly(el)) {
        over = Math.max(el.scrollWidth - el.clientWidth, el.scrollHeight - el.clientHeight - overhang(el));
      } else continue;
      if (over > 1) fail(`The content of ${describe(el)} overflows its box by ${Math.ceil(over)} px: give it room, or split the slide; nothing shrinks.`);
    }

    // --- the shapes
    let svgs = 0;
    for (const marked of [section, ...Array.from(section.querySelectorAll("[data-draw], [data-shot]"))]) {
      marked.removeAttribute("data-draw");
      marked.removeAttribute("data-shot");
    }
    const icons: { name: string; color: string; px: number }[] = [];
    const embedded: { html: string; w: number; h: number }[] = [];
    const shots: Shot[] = [];
    // Elements drawn whole into one picture: their children are in it.
    const whole = new Set<Element>();
    type Box = { x: number; y: number; w: number; h: number };
    type Frame = Box & { rot: number; flip: boolean };
    const box = (el: Element): Box => {
      const r = el.getBoundingClientRect();
      return { x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height };
    };
    const at = (f: Frame) => ({ x: emu(f.x), y: emu(f.y), w: emu(f.w), h: emu(f.h), rot: f.rot, ...(f.flip ? { flipV: true as const } : {}) });
    // A box grown by *by* px on every side, on the slide.
    const region = (b: Box, by = 0) => {
      const x = Math.max(0, b.x - by), y = Math.max(0, b.y - by);
      return { x: emu(x), y: emu(y), w: emu(Math.min(canvas.width, b.x + b.w + by) - x), h: emu(Math.min(canvas.height, b.y + b.h + by) - y) };
    };
    // A picture Chromium takes of *el* over *b* grown by *by* px, once the measuring is done; the element is marked.
    const shot = (el: Element, kind: Shot, b: Box, by = 0): ShapeSpec => {
      el.setAttribute("data-shot", `${el.getAttribute("data-shot") ?? ""} ${shots.length}`.trim());
      shots.push(kind);
      return { kind: "picture", ...region(b, by), rot: 0, src: `shot:${shots.length - 1}` };
    };
    const solid = (css: string): Fill | null => {
      const c = color(css);
      return c ? { type: "solid", color: c } : null;
    };

    // --- fills: a CSS linear gradient as the editor's
    const SIDES: Record<string, [number, boolean]> = {
      "to top": [0, false], "to right": [90, false], "to bottom": [180, false], "to left": [270, false],
      "to top right": [45, true], "to right top": [45, true], "to bottom right": [135, true], "to right bottom": [135, true],
      "to bottom left": [225, true], "to left bottom": [225, true], "to top left": [315, true], "to left top": [315, true],
    };
    const splitTop = (text: string): string[] => {
      const out: string[] = [];
      let depth = 0, start = 0;
      for (let i = 0; i < text.length; i++) {
        if (text[i] === "(") depth++;
        else if (text[i] === ")") depth--;
        else if (text[i] === "," && depth === 0) {
          out.push(text.slice(start, i).trim());
          start = i + 1;
        }
      }
      out.push(text.slice(start).trim());
      return out;
    };
    const angleOf = (text: string): number | null => {
      const m = /^(-?[\d.]+)(deg|turn|rad|grad)$/.exec(text);
      if (!m) return null;
      const n = Number(m[1]);
      const deg = ({ deg: n, turn: n * 360, rad: (n * 180) / Math.PI, grad: n * 0.9 } as Record<string, number>)[m[2]!]!;
      return ((deg % 360) + 360) % 360;
    };
    // A linear gradient over a w by h box as a Fill; null for none; "picture" for what Chromium draws instead: a radial
    // gradient (the editor cannot place its centre and saves it without one, which other readers draw from a corner), a
    // conic one, an image, several layers.
    const gradientOf = (image: string, w: number, h: number): Fill | "picture" | null => {
      if (!image || image === "none") return null;
      const m = /^(repeating-)?linear-gradient\((.*)\)$/s.exec(image.trim());
      if (!m || splitTop(image).length > 1) return "picture";
      const parts = splitTop(m[2]!);
      let angle = 180, scaled = false;
      const side = SIDES[parts[0]!.replace(/\s+/g, " ")], lead = angleOf(parts[0]!);
      if (side) [angle, scaled] = side;
      if (side || lead !== null) parts.shift();
      if (lead !== null) angle = lead;
      // The gradient line's length, which px stop positions are a share of.
      const rad = (angle * Math.PI) / 180, length = Math.abs(w * Math.sin(rad)) + Math.abs(h * Math.cos(rad));
      // One stop per position (a colour with two positions is two), then CSS's fill-in of the missing ones.
      const stops: { color: Color; at: number | null }[] = [];
      for (const part of parts) {
        const c = /^rgba?\([^)]*\)/.exec(part);
        const positions = c ? part.slice(c[0].length).trim().split(/\s+/).filter(Boolean) : [];
        if (!c || positions.some((p) => !/^-?[\d.]+(%|px)$/.test(p))) return "picture";
        if (!positions.length) stops.push({ color: rgba(c[0])!, at: null });
        for (const p of positions) stops.push({ color: rgba(c[0])!, at: p.endsWith("%") ? parseFloat(p) : (parseFloat(p) / length) * 100 });
      }
      if (stops.length < 2) return "picture";
      stops[0]!.at ??= 0;
      stops[stops.length - 1]!.at ??= 100;
      for (let i = 1; i < stops.length; i++) {
        if (stops[i]!.at !== null) {
          stops[i]!.at = Math.max(stops[i]!.at!, stops[i - 1]!.at!);
          continue;
        }
        let j = i;
        while (stops[j]!.at === null) j++;
        const from = stops[i - 1]!.at!, to = stops[j]!.at!;
        for (let k = i; k < j; k++) stops[k]!.at = from + ((to - from) * (k - i + 1)) / (j - i + 1);
      }
      let out = stops as { color: Color; at: number }[];
      if (m[1]) {
        // A repeating gradient: its period, repeated across the whole line as hard stops.
        const period = out[out.length - 1]!.at - out[0]!.at, expanded: typeof out = [];
        for (let base = 0; base < 100 && period > 0; base += period) {
          for (const stop of out) if (base + stop.at <= 100) expanded.push({ color: stop.color, at: base + stop.at });
        }
        if (expanded.length > 200) return "picture";
        out = expanded;
      }
      return { type: "linear", angle, scaled, stops: out.map((s) => ({ color: s.color, at: round(s.at) })) };
    };
    const fillOfStyle = (cs: CSSStyleDeclaration, b: Box): Fill | "picture" | null => {
      if (cs.backgroundClip === "text") return null;   // the gradient is the text's
      return gradientOf(cs.backgroundImage, b.w, b.h) ?? solid(cs.backgroundColor);
    };

    // --- geometry: a box's corners, clamped as CSS clamps radii that overlap
    const radii = (cs: CSSStyleDeclaration, w: number, h: number): number[] => {
      const r = [cs.borderTopLeftRadius, cs.borderTopRightRadius, cs.borderBottomRightRadius, cs.borderBottomLeftRadius]
        .map((v) => (v.endsWith("%") ? (parseFloat(v) / 100) * Math.min(w, h) : parseFloat(v) || 0));
      const f = Math.min(1, w / (r[0]! + r[1]! || 1), h / (r[1]! + r[2]! || 1), w / (r[2]! + r[3]! || 1), h / (r[3]! + r[0]! || 1));
      return r.map((v) => v * f);
    };
    const geometryOf = (cs: CSSStyleDeclaration, w: number, h: number): Geometry => {
      const r = radii(cs, w, h);
      if (r.every((v) => v === 0)) return { preset: "rect" };
      if (cs.borderTopLeftRadius === "50%" && r.every((v) => v === r[0])) return { preset: "ellipse" };
      if (r.every((v) => Math.abs(v - r[0]!) < 0.01)) return { preset: "roundRect", adj: Math.round(Math.min(r[0]! / Math.min(w, h), 0.5) * 100000) };
      const W = emu(w), H = emu(h), R = r.map(emu);
      return { custom: [{ w: W, h: H, fill: true, stroke: true, d: [
        ["M", R[0]!, 0], ["L", W - R[1]!, 0], ["A", R[1]!, R[1]!, 270 * 60000, 90 * 60000],
        ["L", W, H - R[2]!], ["A", R[2]!, R[2]!, 0, 90 * 60000],
        ["L", R[3]!, H], ["A", R[3]!, R[3]!, 90 * 60000, 90 * 60000],
        ["L", 0, R[0]!], ["A", R[0]!, R[0]!, 180 * 60000, 90 * 60000], ["Z"]] }] };
    };
    const shapeGeometry = (kind: string, cs: CSSStyleDeclaration, w: number, h: number): Geometry => {
      if (kind === "ellipse" || kind === "diamond") return { preset: kind };
      const arrow = /^arrow-(right|left|up|down)$/.exec(kind);
      if (arrow) {
        // A shaft half the cross size, the head the last 40% of the length.
        const length = ["right", "left"].includes(arrow[1]!) ? w : h;
        return { preset: `${arrow[1]}Arrow` as "rightArrow" | "leftArrow" | "upArrow" | "downArrow", adj1: 50000, adj2: Math.round(((0.4 * length) / Math.min(w, h)) * 100000) };
      }
      const g = geometryOf(cs, w, h);
      return kind === "rounded" && "preset" in g && g.preset === "rect" ? { preset: "roundRect", adj: Math.round(100000 / 6) } : g;
    };

    // --- strokes. The editor draws a double line as one thick line and has no line per side: those are shapes of
    // their own ("double", "sides").
    const dashOf = (style: string): Stroke["dash"] => (style === "dashed" ? "dash" : style === "dotted" ? "sysDot" : "solid");
    const SIDE_NAMES = ["Top", "Right", "Bottom", "Left"] as const;
    const sidesOf = (cs: CSSStyleDeclaration) => SIDE_NAMES.map((s) => ({ w: parseFloat(cs[`border${s}Width`]), style: cs[`border${s}Style`], color: cs[`border${s}Color`] }));
    const stroke = (cs: CSSStyleDeclaration): Stroke | "double" | "sides" | null => {
      const shown = sidesOf(cs).filter((s) => s.style !== "none" && s.w > 0);
      if (!shown.length) return null;
      if (shown.length < 4 || shown.some((s) => s.w !== shown[0]!.w || s.style !== shown[0]!.style || s.color !== shown[0]!.color)) return "sides";
      if (shown[0]!.style === "double") return "double";
      const c = color(shown[0]!.color);
      return c ? { w: emu(shown[0]!.w), color: c, dash: dashOf(shown[0]!.style) } : null;
    };
    const plain = (line: ReturnType<typeof stroke>): Stroke | null => (typeof line === "string" ? null : line);
    // A frame's part, laid out in the frame's own coordinates, carried along by its turn and flip.
    const partOf = (f: Frame, x: number, y: number, w: number, h: number): Frame => {
      const fx = f.x + f.w / 2, fy = f.y + f.h / 2, a = (f.rot * Math.PI) / 180;
      const dx = x + w / 2 - fx, dy = (f.flip ? -1 : 1) * (y + h / 2 - fy);
      const cx = fx + dx * Math.cos(a) - dy * Math.sin(a), cy = fy + dx * Math.sin(a) + dy * Math.cos(a);
      return { x: cx - w / 2, y: cy - h / 2, w, h, rot: f.rot, flip: f.flip };
    };
    // The shapes a "double" or "sides" border adds after its box. A double border is CSS's two lines a third of its
    // width each: two rectangles, the line centred a sixth in from each edge of the border. The builder insets a stroked
    // shape by half its line, so the outer rectangle is the path's edge. Sides are a filled strip each when solid, a
    // line each when dashed or dotted.
    const borders = (cs: CSSStyleDeclaration, f: Frame, line: ReturnType<typeof stroke>): ShapeSpec[] => {
      if (line === "double") {
        const bw = parseFloat(cs.borderTopWidth), W = emu(f.w - bw / 3), H = emu(f.h - bw / 3), i = emu((2 * bw) / 3);
        const rect = (n: number) => ({ w: W, h: H, fill: false, stroke: true, d: [["M", n, n], ["L", W - n, n], ["L", W - n, H - n], ["L", n, H - n], ["Z"]] });
        return [{ kind: "shape", ...at(f), geometry: { custom: [rect(0), rect(i)] }, fill: null, stroke: { w: emu(bw / 3), color: color(cs.borderTopColor) ?? { hex: "#000000", alpha: 1 }, dash: "solid" } }];
      }
      if (line !== "sides") return [];
      const out: ShapeSpec[] = [];
      sidesOf(cs).forEach((s, i) => {
        const c = color(s.color);
        if (s.style === "none" || !(s.w > 0) || !c) return;
        const dashed = s.style === "dashed" || s.style === "dotted";
        // Top, right, bottom, left: the strip, or the line down its middle.
        const strip = [[0, 0, f.w, s.w], [f.w - s.w, 0, s.w, f.h], [0, f.h - s.w, f.w, s.w], [0, 0, s.w, f.h]][i]!;
        const across = i % 2 === 0;
        const [x, y, w, h] = dashed ? (across ? [strip[0]!, strip[1]! + s.w / 2, strip[2]!, 0] : [strip[0]! + s.w / 2, strip[1]!, 0, strip[3]!]) : strip;
        const part = at(partOf(f, f.x + x!, f.y + y!, w!, h!));
        out.push(dashed
          ? { kind: "shape", ...part, geometry: { preset: "line" }, fill: null, stroke: { w: emu(s.w), color: c, dash: dashOf(s.style) } }
          : { kind: "shape", ...part, geometry: { preset: "rect" }, fill: { type: "solid", color: c }, stroke: null });
      });
      return out;
    };

    // --- transforms: about the box's centre, as OOXML turns a shape
    type Transform = { tx: number; ty: number; rot: number; sx: number; sy: number; skew: number; flip: boolean };
    const transformOf = (el: Element): Transform | null => {
      const m = /^matrix\(([^)]+)\)$/.exec(getComputedStyle(el).transform);
      if (!m) return null;   // none, or a 3-D matrix, which is drawn whole
      const [a, b, c, d, e, f] = m[1]!.split(",").map(Number) as [number, number, number, number, number, number];
      // As a rotation of a shear of a scale; a negative determinant is a flip, drawn first as OOXML draws it. The style
      // gives the matrix to six places: the scales are rounded to four.
      const sx = Math.hypot(a, b), det = a * d - b * c, four = (v: number) => Math.round(v * 10000) / 10000;
      return { tx: e, ty: f, rot: ((Math.round(((Math.atan2(b, a) * 180) / Math.PI) * 100) / 100) % 360 + 360) % 360, sx: four(sx), sy: sx ? four(Math.abs(det) / sx) : 0,
        skew: det ? (Math.atan((a * c + b * d) / det) * 180) / Math.PI : 0, flip: det < 0 };
    };
    // The box as laid out before its transform (own() takes the transform off while it measures), then moved, scaled
    // and turned by it.
    const frame = (el: Element, t: Transform | null): Frame => {
      const b = box(el);
      if (!t) return { ...b, rot: 0, flip: false };
      const w = b.w * t.sx, h = b.h * t.sy;
      return { x: b.x + b.w / 2 + t.tx - w / 2, y: b.y + b.h / 2 + t.ty - h / 2, w, h, rot: t.rot, flip: t.flip };
    };

    // --- shadows
    // A box-shadow the editor draws: one outer shadow without blur. "picture" for an outer one it cannot; "inset".
    const shadowOf = (cs: CSSStyleDeclaration): Shadow | "picture" | "inset" | null => {
      if (!cs.boxShadow || cs.boxShadow === "none") return null;
      if (/\binset\b/.test(cs.boxShadow)) return "inset";
      const all = splitTop(cs.boxShadow);
      const m = /^(rgba?\([^)]*\))\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px\s+(-?[\d.]+)px$/.exec(all[0]!);
      if (all.length > 1 || !m || Number(m[4]) > 0) return "picture";
      const c = color(m[1]!);
      return c ? { x: emu(Number(m[2])), y: emu(Number(m[3])), spread: emu(Number(m[5])), color: c } : null;
    };
    // How far shadows reach past their box: offset, blur and spread (CSS spreads a blur of B over B px).
    const reach = (list: string): number => {
      if (!list || list === "none") return 0;
      return Math.max(0, ...splitTop(list).map((one) => {
        if (/\binset\b/.test(one)) return 0;
        const n = (one.replace(/rgba?\([^)]*\)/, "").match(/-?[\d.]+px/g) ?? []).map(parseFloat);
        return Math.max(Math.abs(n[0] ?? 0), Math.abs(n[1] ?? 0)) + (n[2] ?? 0) + Math.max(0, n[3] ?? 0);
      }));
    };
    // How far a filter paints past its box: a blur of B over 3B px, a drop shadow as a shadow.
    const filterReach = (filter: string): number =>
      [...filter.matchAll(/blur\(([\d.]+)px\)/g)].reduce((sum, m) => sum + 3 * Number(m[1]), 0) +
      [...filter.matchAll(/drop-shadow\((rgba?\([^)]*\)[^)]*)\)/g)].reduce((sum, m) => sum + reach(m[1]!), 0);

    // --- what only a picture shows
    const isContainer = (el: Element): boolean => (tagOf(el) === "DIV" && !isText(el)) || tagOf(el) === "SECTION";
    const drawnWhole = (el: Element, t: Transform | null): boolean => {
      const tag = tagOf(el), cs = getComputedStyle(el);
      // Drawn from the icon set, on a page of its own, or as a path: nothing of it is painted here to take.
      if (["X-ICON", "X-EMBED", "X-CONNECTOR"].includes(tag)) return false;
      // The editor saves filters and blend modes, then draws the plain shape.
      if (cs.filter !== "none" || cs.backdropFilter !== "none" || cs.mixBlendMode !== "normal" || /^matrix3d/.test(cs.transform)) return true;
      const children = isContainer(el) && el.children.length > 0;
      // CSS fades a container with its children as one; OOXML has no opacity over a group.
      if (parseFloat(cs.opacity) < 1 && children) return true;
      if (!t) return false;
      const scaled = Math.abs(t.sx - 1) > 0.001 || Math.abs(t.sy - 1) > 0.001, skewed = Math.abs(t.skew) > 0.01;
      // Children are measured by their turned bounds, and a table cannot turn.
      if (children || tag === "TABLE") return t.rot !== 0 || scaled || skewed || t.flip;
      // Text keeps its size and is never mirrored in the editor: only a turned text box is native.
      if (isText(el)) return scaled || skewed || t.flip;
      if (tag === "IMG") return skewed || Math.abs(t.sx - t.sy) > 0.001;
      // A skewed box is a parallelogram only as an unturned rectangle with one plain line.
      if (skewed) return t.rot !== 0 || t.flip || tag === "HR" || (tag === "X-SHAPE" && el.getAttribute("kind") !== "rect") || radii(cs, 1, 1).some((v) => v > 0) || typeof stroke(cs) === "string";
      return false;
    };

    // --- motion: builds on pinned children, a magic-move name
    const motion = (el: Element): { magic?: string; build?: Build } => {
      if (!pinned(el)) return {};
      const read = (value: string | null) => {
        if (!value) return undefined;
        const [effect, ...rest] = value.trim().split(/\s+/);
        return { effect: effect!, order: rest.includes("order") ? Number(rest[rest.indexOf("order") + 1]) || 0 : 0, auto: rest.includes("auto") };
      };
      const into = read(el.getAttribute("data-build-in")), out = read(el.getAttribute("data-build-out"));
      return { ...(el.id ? { magic: el.id } : {}), ...(into || out ? { build: { ...(into ? { in: into } : {}), ...(out ? { out } : {}) } } : {}) };
    };

    const runs = (container: Element): Run[] => {
      const out: Run[] = [];
      const add = (text: string, from: Element) => {
        const cs = getComputedStyle(from);
        let underline = false, strike = false, opacity = 1;
        for (let at: Element | null = from; at && at !== container.parentElement; at = at.parentElement) {
          const line = getComputedStyle(at).textDecorationLine;
          if (line.includes("underline")) underline = true;
          if (line.includes("line-through")) strike = true;
        }
        // CSS fades an element with everything in it.
        for (let at: Element | null = from; at && at !== section.parentElement; at = at.parentElement) opacity *= parseFloat(getComputedStyle(at).opacity);
        const run: Run = { text, size: round(parseFloat(cs.fontSize) * ptPerPx), font: family(cs), color: color(cs.color) ?? { hex: "#000000", alpha: 1 } };
        if (parseInt(cs.fontWeight, 10) >= 600) run.b = true;
        if (cs.fontStyle === "italic") run.i = true;
        if (underline) run.u = true;
        if (strike) run.s = true;
        const spacing = parseFloat(cs.letterSpacing);
        if (spacing) run.spacing = round(spacing * ptPerPx);
        // OOXML has only all caps: lower case and capitals are the text itself.
        if (cs.textTransform === "uppercase") run.caps = true;
        if (cs.textTransform === "lowercase") run.text = run.text.toLowerCase();
        if (cs.textTransform === "capitalize") {
          const before = out[out.length - 1]?.text ?? "";
          run.text = run.text.replace(/(^|\s)(\p{L})/gu, (all, space: string, letter: string, offset: number) => (offset === 0 && before && !/\s$/.test(before) ? all : space + letter.toUpperCase()));
        }
        const outline = parseFloat(cs.getPropertyValue("-webkit-text-stroke-width"));
        if (outline > 0) run.outline = { w: emu(outline), color: color(cs.getPropertyValue("-webkit-text-stroke-color")) ?? run.color, dash: "solid" };
        // Gradient text: the gradient of the element that clips its background to its text.
        for (let at: Element | null = from; at && at !== container.parentElement; at = at.parentElement) {
          const clip = getComputedStyle(at);
          if (clip.backgroundClip !== "text") continue;
          const g = gradientOf(clip.backgroundImage, box(at).w, box(at).h);
          if (g && g !== "picture") run.gradient = g;
          break;
        }
        if (opacity < 1) run.color = { ...run.color, alpha: round(run.color.alpha * opacity) };
        const link = from.closest("a");
        if (link && container.contains(link)) run.href = link.getAttribute("href")!;
        out.push(run);
      };
      const walk = (node: Node) => {
        for (const child of Array.from(node.childNodes)) {
          if (child.nodeType === Node.TEXT_NODE) {
            const text = (child.textContent ?? "").replace(/[ \t\n\r\f]+/g, " ");
            if (text) add(text, child.parentElement!);
          } else if (child.nodeType === Node.ELEMENT_NODE) {
            if (getComputedStyle(child as Element).display === "none") continue;
            if (tagOf(child as Element) === "BR") add("\n", child as Element);
            else walk(child);
          }
        }
      };
      walk(container);
      // As layout collapses them: no space at a paragraph's start or end, nor beside a line break.
      for (let i = 0; i < out.length; i++) {
        if (i === 0 || out[i - 1]!.text.endsWith("\n")) out[i]!.text = out[i]!.text.replace(/^ /, "");
        if (i === out.length - 1 || out[i + 1]!.text.startsWith("\n")) out[i]!.text = out[i]!.text.replace(/ $/, "");
      }
      const merged: Run[] = [];
      for (const run of out.filter((r) => r.text)) {
        const last = merged[merged.length - 1];
        if (last && JSON.stringify({ ...last, text: "" }) === JSON.stringify({ ...run, text: "" })) last.text += run.text;
        else merged.push(run);
      }
      return merged;
    };
    const paragraph = (el: Element): Para => {
      const cs = getComputedStyle(el);
      const size = parseFloat(cs.fontSize);
      const lineHeight = parseFloat(cs.lineHeight);
      const align = ({ left: "left", start: "left", center: "center", right: "right", end: "right", justify: "justify" } as Record<string, string>)[cs.textAlign] ?? "left";
      // The editor's single spacing is 1.2 times the font size. A margin moves the box, measured where it lands; between
      // the paragraphs of one box, textShape gives it as spacing before.
      return { align, line: Number.isFinite(lineHeight) ? Math.round((lineHeight / (1.2 * size)) * 1000) / 1000 : 1, before: 0, after: 0, runs: runs(el) };
    };
    // What a text box holds: a hidden item draws nothing and takes no room.
    const shownChildren = (el: Element): Element[] => Array.from(el.children).filter((child) => getComputedStyle(child).display !== "none");
    const listParas = (list: Element, left: number, indent?: number): Para[] => {
      const cs = getComputedStyle(list);
      const ordered = tagOf(list) === "OL";
      return shownChildren(list).map((li) => {
        const para = paragraph(li);
        // The item's text starts where its box does; its marker hangs in the list's left padding.
        para.indent = twips(indent ?? box(li).x - left);
        para.hanging = twips(indent ?? parseFloat(cs.paddingLeft));
        if (ordered) {
          para.numbered = true;
          para.start = 1;
        } else {
          para.bullet = ({ none: "", square: "▪", circle: "◦" } as Record<string, string>)[cs.listStyleType] ?? "•";
        }
        return para;
      });
    };
    // How a box is painted natively: its fill and its border (none when a picture of it is drawn instead).
    type Look = { fill: Fill | null; line: ReturnType<typeof stroke> };
    // A text element is one shape: its box, its fill and border, and its padding as the text's insets.
    const textShape = (el: Element, f: Frame, look: Look): ShapeSpec[] => {
      const cs = getComputedStyle(el);
      const b = box(el);
      const line = plain(look.line);
      const side = (padding: string, border: string) => emu(parseFloat(padding) + (line ? 0 : parseFloat(border) || 0));
      const tag = tagOf(el);
      let paras: Para[];
      let items: Element[] = [];
      if (tag === "UL" || tag === "OL") {
        paras = listParas(el, b.x);
        items = shownChildren(el);
      } else if (tag === "DIV" && !textOnly(el)) {
        const indent = Number(el.getAttribute("data-text")) || undefined;
        paras = shownChildren(el).flatMap((child) => (["UL", "OL"].includes(tagOf(child)) ? listParas(child, b.x, indent) : [paragraph(child)]));
        items = shownChildren(el).flatMap((child) => (["UL", "OL"].includes(tagOf(child)) ? shownChildren(child) : [child]));
      } else paras = [paragraph(el)];
      // The space margins or a gap leave above each paragraph of the box, as Chromium laid it out.
      let top = b.y + parseFloat(cs.borderTopWidth) + parseFloat(cs.paddingTop);
      items.forEach((item, i) => {
        const r = box(item);
        paras[i]!.before = round(Math.max(0, r.y - top) * ptPerPx);
        top = r.y + r.h;
      });
      return [{
        kind: "shape", ...at(f), geometry: geometryOf(cs, b.w, b.h), fill: look.fill, stroke: line,
        text: {
          insets: [side(cs.paddingLeft, cs.borderLeftWidth), side(cs.paddingTop, cs.borderTopWidth), side(cs.paddingRight, cs.borderRightWidth), side(cs.paddingBottom, cs.borderBottomWidth)],
          anchor: (el.getAttribute("data-anchor") as "ctr" | "b" | null) ?? "t", wrap: cs.whiteSpace !== "nowrap", paras,
        },
      }, ...borders(cs, f, look.line)];
    };
    // A painted div or an x-shape: its geometry, fill and border. A skewed one is a parallelogram.
    const painted = (el: Element, f: Frame, look: Look, t: Transform | null): ShapeSpec[] => {
      const cs = getComputedStyle(el), b = box(el);
      const kind = tagOf(el) === "X-SHAPE" ? el.getAttribute("kind")! : "rect";
      if (kind === "line") return [rule(el, f)];
      if (!look.fill && !look.line) return [];
      let geometry = shapeGeometry(kind, cs, b.w, b.h), place = f;
      if (t && Math.abs(t.skew) > 0.01) {
        // skewX about the centre: the top edge shifts by -dx, the bottom by dx.
        const dx = (Math.tan((t.skew * Math.PI) / 180) * f.h) / 2, D = emu(2 * Math.abs(dx));
        place = { ...f, x: f.x - Math.abs(dx), w: f.w + 2 * Math.abs(dx) };
        const W = emu(place.w), H = emu(f.h);
        geometry = { custom: [{ w: W, h: H, fill: true, stroke: true, d: dx < 0
          ? [["M", D, 0], ["L", W, 0], ["L", W - D, H], ["L", 0, H], ["Z"]]
          : [["M", 0, 0], ["L", W - D, 0], ["L", W, H], ["L", D, H], ["Z"]] }] };
      }
      // Only rectangles take the border shapes.
      const boxed = "custom" in geometry || ["rect", "roundRect"].includes(geometry.preset);
      return [{ kind: "shape", ...at(place), geometry, fill: look.fill, stroke: plain(look.line) }, ...(boxed ? borders(cs, f, look.line) : [])];
    };
    // An hr, or an x-shape line: a line across the top of its box, as thick as its top border (2 px without one) and
    // where that border is.
    const rule = (el: Element, f: Frame): ShapeSpec => {
      const cs = getComputedStyle(el), shown = cs.borderTopStyle !== "none" && parseFloat(cs.borderTopWidth) > 0;
      const w = shown ? parseFloat(cs.borderTopWidth) : 2;
      const c = color(shown ? cs.borderTopColor : cs.color) ?? { hex: "#000000", alpha: 1 };
      return { kind: "shape", ...at({ ...f, y: f.y + w / 2, h: 0 }), geometry: { preset: "line" }, fill: null, stroke: { w: emu(w), color: c, dash: dashOf(cs.borderTopStyle) } };
    };
    // A line or an open polyline between two points of its host (px, or % of the host's box from its padding edge),
    // routed straight, across then down (hv), down then across (vh) or through the middle (elbow).
    const connector = (el: Element): ShapeSpec[] => {
      const cs = getComputedStyle(el);
      let host = el.parentElement!;
      while (!isHost(host)) host = host.parentElement!;
      const hb = box(host), hs = getComputedStyle(host);
      const left = hb.x + parseFloat(hs.borderLeftWidth), top = hb.y + parseFloat(hs.borderTopWidth);
      const width = hb.w - parseFloat(hs.borderLeftWidth) - parseFloat(hs.borderRightWidth), height = hb.h - parseFloat(hs.borderTopWidth) - parseFloat(hs.borderBottomWidth);
      const coord = (name: string, size: number) => {
        const v = el.getAttribute(name)!.trim();
        return v.endsWith("%") ? (parseFloat(v) / 100) * size : parseFloat(v);
      };
      let x1: number, y1: number, x2: number, y2: number;
      if (el.hasAttribute("x1")) [x1, y1, x2, y2] = [left + coord("x1", width), top + coord("y1", height), left + coord("x2", width), top + coord("y2", height)];
      else {
        // Without points it runs corner to corner across its own box, from the corner "from" names.
        const b = box(el), from = el.getAttribute("from") ?? "tl";
        [x1, x2] = from.endsWith("l") ? [b.x, b.x + b.w] : [b.x + b.w, b.x];
        [y1, y2] = from.startsWith("t") ? [b.y, b.y + b.h] : [b.y + b.h, b.y];
      }
      const xm = (x1 + x2) / 2, route = el.getAttribute("route") ?? "straight";
      const raw = route === "hv" ? [[x1, y1], [x2, y1], [x2, y2]] : route === "vh" ? [[x1, y1], [x1, y2], [x2, y2]]
        : route === "elbow" ? [[x1, y1], [xm, y1], [xm, y2], [x2, y2]] : [[x1, y1], [x2, y2]];
      // A point that repeats the last, or that the next continues in a straight line, is no corner.
      const points: number[][] = [];
      for (const p of raw.map(([x, y]) => [emu(x!), emu(y!)])) {
        const last = points[points.length - 1], prev = points[points.length - 2];
        if (last && last[0] === p[0] && last[1] === p[1]) continue;
        if (prev && last && (last[0]! - prev[0]!) * (p[1]! - last[1]!) === (last[1]! - prev[1]!) * (p[0]! - last[0]!)) points.pop();
        points.push(p);
      }
      if (points.length < 2) return [];
      const head = el.getAttribute("head") ?? "end";
      const end = (named: string | null, on: boolean) => (named ?? (on ? "filled" : "none")) as "none" | "open" | "filled";
      const c = color(cs.color) ?? { hex: "#000000", alpha: 1 };
      const xs = points.map((p) => p[0]!), ys = points.map((p) => p[1]!);
      return [{
        kind: "path", x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys), rot: 0, points,
        // Its width is what the style says: a border with no style computes to none.
        stroke: { w: emu(parseFloat((el as HTMLElement).style.borderTopWidth) || 2), color: { ...c, alpha: round(c.alpha * parseFloat(cs.opacity)) }, dash: dashOf(cs.borderTopStyle) },
        head: end(el.getAttribute("head-start"), head === "start" || head === "both"), tail: end(el.getAttribute("head-end"), head === "end" || head === "both"),
      }];
    };
    const picture = (el: HTMLImageElement, f: Frame): ShapeSpec => {
      const cs = getComputedStyle(el);
      const nw = el.naturalWidth || f.w, nh = el.naturalHeight || f.h;
      const out: Extract<ShapeSpec, { kind: "picture" }> = { kind: "picture", ...at(f), src: el.src };
      let crop = [0, 0, 0, 0];
      if (cs.objectFit === "contain") {
        const scale = Math.min(f.w / nw, f.h / nh), w = nw * scale, h = nh * scale;
        Object.assign(out, at({ ...f, x: f.x + (f.w - w) / 2, y: f.y + (f.h - h) / 2, w, h }));
      } else if (cs.objectFit === "cover") {
        const scale = Math.max(f.w / nw, f.h / nh);
        const cx = (100 * (1 - f.w / (nw * scale))) / 2, cy = (100 * (1 - f.h / (nh * scale))) / 2;
        crop = [cx, cy, cx, cy];
      }
      const zoom = el.getAttribute("data-crop");
      if (zoom) {
        // ZOOM X% Y%: 1/ZOOM of the picture as fitted shows, X% of what is cut off is cut from the left, Y% from the top.
        const [z = 1, x = 50, y = 50] = zoom.trim().split(/\s+/).map(parseFloat);
        const cutW = (100 - crop[0]! - crop[2]!) * (1 - 1 / z), cutH = (100 - crop[1]! - crop[3]!) * (1 - 1 / z);
        crop = [crop[0]! + (cutW * x) / 100, crop[1]! + (cutH * y) / 100, crop[2]! + (cutW * (100 - x)) / 100, crop[3]! + (cutH * (100 - y)) / 100];
      }
      if (crop.some((v) => v)) out.crop = crop.map(round);
      const g = geometryOf(cs, f.w, f.h);
      if ("preset" in g && g.preset === "roundRect") out.radius = g.adj;
      return out;
    };
    const table = (el: HTMLTableElement, f: Frame): ShapeSpec => {
      const rows = Array.from(el.rows);
      if (!rows.length) fail(`${describe(el)} has no rows.`);
      const first = Array.from(rows[0]!.cells);
      const pad = getComputedStyle(first[0]!);
      return {
        kind: "table", ...at(f), cols: first.map((c) => emu(box(c).w)),
        pad: [pad.paddingTop, pad.paddingRight, pad.paddingBottom, pad.paddingLeft].map((v) => twips(parseFloat(v))),
        rows: rows.map((row) => {
          if (row.cells.length !== first.length) fail(`${describe(row)}: every row has as many cells as the first.`);
          return {
            h: emu(box(row).h), fill: color(getComputedStyle(row).backgroundColor),
            // A cell's border is one line all round.
            cells: Array.from(row.cells).map((c) => ({ paras: [paragraph(c)], fill: color(getComputedStyle(c).backgroundColor), border: plain(stroke(getComputedStyle(c))) })),
          };
        }),
      };
    };
    // What one element draws by itself, as one drawing (a group when it takes several); a container's children are
    // laid out by layer().
    const heading = Array.from(section.querySelectorAll("h1, h2, h3")).find((h) => !inside(h));
    const own = (el: Element): ShapeSpec[] => {
      const cs = getComputedStyle(el), outer = box(el), t = transformOf(el);
      if (drawnWhole(el, t)) {
        // Nothing inside the picture is native: a connector (which this page does not paint) or a build would be lost.
        const lost = Array.from(el.querySelectorAll("x-connector, [data-build-in], [data-build-out]")).find((d) => !inside(d));
        if (lost) fail(`${describe(lost)} is inside ${describe(el)}, which goes in as one picture (a filter, a blend mode, or opacity or a turn over its children): pin it beside the box.`);
        whole.add(el);
        if (outer.w < 1 || outer.h < 1) return [];
        const under = cs.mixBlendMode !== "normal" || cs.backdropFilter !== "none";
        return [shot(el, under ? "region" : "element", outer, filterReach(cs.filter))];
      }
      const style = (el as HTMLElement).style, saved = style.transform;
      if (t) style.transform = "none";
      try {
        return drawn(el, cs, outer, t);
      } finally {
        if (t) style.transform = saved;
      }
    };
    const drawn = (el: Element, cs: CSSStyleDeclaration, outer: Box, t: Transform | null): ShapeSpec[] => {
      const tag = tagOf(el), f = frame(el, t);
      if (tag === "SVG") {
        // Node finds the svg it draws by this mark: an svg inside an embed or another svg is no picture of its own. It
        // is drawn as it shows, turned or not.
        el.setAttribute("data-draw", String(svgs));
        return [{ kind: "picture", ...region(outer), rot: 0, src: `svg:${svgs++}` }];
      }
      const opacity = parseFloat(cs.opacity);
      const faded = <T extends ShapeSpec>(shape: T): T => (opacity < 1 ? { ...shape, opacity } : shape);
      if (tag === "X-ICON") {
        icons.push({ name: el.getAttribute("name")!, color: color(cs.color)?.hex ?? "#000000", px: f.w });
        return [faded({ kind: "picture", ...at(f), src: `icon:${icons.length - 1}` })];
      }
      if (tag === "X-EMBED") {
        const b = box(el);
        embedded.push({ html: el.innerHTML, w: b.w, h: b.h });
        return [faded({ kind: "picture", ...at(f), src: `embed:${embedded.length - 1}` })];
      }
      if (tag === "X-CONNECTOR") return connector(el);
      if (tag === "TABLE") return [table(el as HTMLTableElement, f)];
      if (!isText(el) && !["DIV", "X-SHAPE", "HR", "IMG"].includes(tag)) return [];
      const background = ["IMG", "HR"].includes(tag) ? null : fillOfStyle(cs, box(el));
      const shadow = tag === "HR" ? null : shadowOf(cs);
      const textReach = isText(el) ? Math.max(...[el, ...Array.from(el.querySelectorAll("*"))].map((e) => reach(getComputedStyle(e).textShadow))) : 0;
      // What the editor cannot draw of a box is one picture of the box, its text hidden, behind the native text: a radial
      // background, an inset shadow (saved, drawn by nobody), a text shadow (a run has none). Nothing is drawn twice.
      const bare = tag !== "IMG" && (background === "picture" || shadow === "inset" || textReach > 0);
      const behind = bare ? [shot(el, isText(el) ? "text" : "box", outer, Math.max(reach(cs.boxShadow), textReach))] : [];
      const look: Look = bare ? { fill: null, line: null } : { fill: background as Fill | null, line: stroke(cs) };
      const shapes = tag === "IMG" ? [picture(el as HTMLImageElement, f)] : isText(el) ? textShape(el, f, look) : tag === "HR" ? [rule(el, f)] : painted(el, f, look, t);
      // The slide's first heading names it in the outline, before any text above it.
      if (el === heading && shapes[0]) shapes[0].name = "Title";
      if (!bare && shadow) {
        // The editor draws an unblurred shadow under what is filled or stroked; other readers put an unfilled text
        // box's shadow on its text. Any other shadow is Chromium's picture of it behind the box.
        const main = shapes[0];
        if (typeof shadow === "object" && main && (main.kind === "picture" || (main.kind === "shape" && (main.fill || main.stroke)))) main.shadow = shadow;
        else behind.push(shot(el, "shadow", outer, reach(cs.boxShadow)));
      }
      const all = [...behind, ...shapes.map((s) => (s.kind === "shape" || s.kind === "picture" ? faded(s) : s))];
      return all.length > 1 ? [{ kind: "group", ...region(outer), rot: 0, children: all }] : all;
    };
    const tabled = (list: ShapeSpec[]): boolean => list.some((s) => s.kind === "table" || (s.kind === "group" && tabled(s.children)));
    // A pinned element's builds and magic-move name go on everything it draws: one drawing, or a group of them. What it
    // groups moves with it, so its own build or magic-move name would be lost; and the editor groups no table.
    const moving = (el: Element, shapes: ShapeSpec[]): ShapeSpec[] => {
      const m = motion(el);
      if ((!m.magic && !m.build) || !shapes.length) return shapes;
      for (const d of Array.from(el.querySelectorAll("[data-build-in], [data-build-out], [id]"))) {
        if (inside(d) || !pinned(d)) continue;
        if (d.hasAttribute("data-build-in") || d.hasAttribute("data-build-out")) fail(`${describe(d)} has a build inside ${describe(el)}, which moves as one drawing: builds go on the section's pinned children.`);
        // An id is only ever a magic-move name, whichever of the two slides arrives by the move.
        if (d.id) fail(`${describe(d)} has an id inside ${describe(el)}, which moves as one drawing: magic-move ids go on the section's pinned children.`);
      }
      if (shapes.length > 1 && tabled(shapes)) fail(`${describe(el)} moves as one group (it has a build or an id), and a table cannot join a group: give the table its own motion, or move it out of the moving box.`);
      return [Object.assign(shapes.length === 1 ? shapes[0]! : { kind: "group" as const, ...region(box(el)), rot: 0, children: shapes }, m)];
    };
    // Claude Slides' paint order: in a host, its flow content is one layer where its first flow child sits. In the
    // section, pinned children listed before that layer paint below it and the rest above; in any other host (a
    // position:relative or pinned div) pinned children always paint over its flow.
    const layer = (host: Element): ShapeSpec[] => {
      const below: ShapeSpec[] = [], flow: ShapeSpec[] = [], above: ShapeSpec[] = [];
      let seen = false;
      // own() first: it says whether the element is drawn whole, its children with it.
      const inner = (el: Element) => isContainer(el) && !whole.has(el);
      const walk = (parent: Element) => {
        for (const child of Array.from(parent.children)) {
          if (tagOf(child) === "ASIDE" || getComputedStyle(child).display === "none") continue;
          if (pinned(child)) {
            const mine = own(child);
            (seen || host !== section ? above : below).push(...moving(child, [...mine, ...(inner(child) ? layer(child) : [])]));
            continue;
          }
          seen = true;
          flow.push(...own(child));
          if (inner(child)) flow.push(...(isHost(child) ? layer(child) : (walk(child), [])));
        }
      };
      walk(host);
      return [...below, ...flow, ...above];
    };
    const sectionStyle = getComputedStyle(section), sectionBox = box(section);
    const background = fillOfStyle(sectionStyle, sectionBox);
    const transition = section.getAttribute("data-transition");
    const spec: SlideSpec = {
      background: background === "picture" ? null : background,
      shapes: [...(background === "picture" ? [shot(section, "box", sectionBox)] : []), ...layer(section)],
      notes: (section.querySelector(":scope > aside")?.textContent ?? "").trim(), hidden: section.hasAttribute("hidden"),
      transition: transition ? { effect: transition, ms: transition === "magic" ? 1000 : 700 } : null,
    };
    return { spec, icons, embeds: embedded, shots };
  } catch (error) {
    if (error instanceof Refused) return { error: error.message };
    throw error;
  }
}
