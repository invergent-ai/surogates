({selector, base}) => {
const __ROLE_BY_TAG = {
  a: 'link', button: 'button', textarea: 'textbox', select: 'combobox',
  h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading',
  h5: 'heading', h6: 'heading',
  img: 'img', p: 'paragraph',
  ul: 'list', ol: 'list', dl: 'list', li: 'listitem',
  dt: 'term', dd: 'definition',
  table: 'table', tr: 'row', td: 'cell', th: 'columnheader',
  thead: 'rowgroup', tbody: 'rowgroup', tfoot: 'rowgroup',
  nav: 'navigation', main: 'main', aside: 'complementary',
  article: 'article', section: 'region', form: 'form', dialog: 'dialog',
  option: 'option', summary: 'button', details: 'group',
  fieldset: 'group', label: 'label', legend: 'legend',
  iframe: 'iframe', video: 'video', audio: 'audio',
  progress: 'progressbar', hr: 'separator',
};

const __ROLE_BY_INPUT_TYPE = {
  button: 'button', submit: 'button', reset: 'button', image: 'button',
  checkbox: 'checkbox', radio: 'radio', range: 'slider',
  file: 'file-input', hidden: 'hidden',
  search: 'searchbox', number: 'spinbutton',
};

function roleOf(el) {
  // An explicit role wins, and the attribute is a space-separated fallback
  // list of which only the first token applies.
  const explicit = el.getAttribute('role');
  if (explicit) {
    const first = explicit.trim().split(/\s+/)[0];
    if (first) return first;
  }
  const tag = el.tagName.toLowerCase();
  if (tag === 'input') {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    return __ROLE_BY_INPUT_TYPE[type] || 'textbox';
  }
  // A bare anchor is a jump target, not a link.
  if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
  const editable = el.getAttribute('contenteditable');
  if (editable === '' || editable === 'true') return 'textbox';
  // header/footer are only landmarks at the top level; inside an article or
  // section they are that section's own header, not the page banner.
  if (tag === 'header' || tag === 'footer') {
    return el.closest('article, section, aside, nav')
      ? 'generic'
      : (tag === 'header' ? 'banner' : 'contentinfo');
  }
  return __ROLE_BY_TAG[tag] || 'generic';
}

// Roles whose accessible name comes from their own text content.  Everything
// else -- main, nav, form, region, list, generic -- is a container: naming it
// by its contents swallows the page into one string, which then poisons the
// role+name ref healing that reads these names back.  Roles that merely
// CARRY text (cell, listitem, paragraph) are deliberately absent: their text
// reaches the model through text_block, and naming them too would restore the
// per-element innerText layout cost this set exists to avoid.
const __NAME_FROM_CONTENT = new Set(['button','link','heading','option','tab',
  'menuitem','menuitemcheckbox','menuitemradio','switch','label','legend']);

function clean240(s) {
  return String(s || '').replace(/\s+/g, ' ').trim().slice(0, 240);
}

function nameOf(el, role) {
  const aria = el.getAttribute('aria-label');
  if (aria) return clean240(aria);
  const labelledby = el.getAttribute('aria-labelledby');
  if (labelledby) {
    const parts = [];
    for (const id of labelledby.trim().split(/\s+/)) {
      const target = document.getElementById(id);
      if (target) parts.push(target.innerText || target.textContent || '');
    }
    const joined = clean240(parts.join(' '));
    if (joined) return joined;
  }
  const tag = el.tagName.toLowerCase();
  if (tag === 'img') return clean240(el.getAttribute('alt') || '');
  if (tag === 'iframe') {
    return clean240(el.getAttribute('title') || el.getAttribute('name') || '');
  }
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    // el.labels is the platform's own answer for both <label for=...> and a
    // wrapping <label>, so there is nothing to walk by hand.
    const labels = el.labels ? Array.from(el.labels) : [];
    if (labels.length) {
      const named = clean240(
        labels.map((l) => l.innerText || l.textContent || '').join(' ')
      );
      if (named) return named;
    }
    return clean240(el.getAttribute('placeholder')
      || el.getAttribute('title')
      || el.getAttribute('name')
      || '');
  }
  if (__NAME_FROM_CONTENT.has(role)) {
    return clean240(el.innerText || el.textContent || '');
  }
  // Containers: an explicit label only, never their contents.
  return clean240(el.getAttribute('title') || '');
}

function depthOf(el) {
  let d = 0, cur = el;
  while (cur && cur.parentElement) { d++; cur = cur.parentElement; }
  return d;
}

function isBlockLevel(el) {
  // Reads the precomputed __style map -- getComputedStyle here would run once
  // per scanned descendant and force layout each time.
  const s = __style.get(el);
  const d = s ? s.display : 'block';
  return d !== 'inline' && d !== 'inline-block' && d !== 'contents' && d !== 'none';
}

const __INTERACTIVE = new Set(['button','link','textbox','combobox','checkbox',
  'radio','menuitem','tab','switch','searchbox','slider','spinbutton',
  'option','file-input']);

function isTextBlock(el) {
  // A text block is an element whose subtree holds no interactive element and
  // no block-level element -- i.e. pure inline markup, so its innerText reads
  // as one coherent run.
  for (const child of Array.from(el.querySelectorAll('*'))) {
    if (__INTERACTIVE.has(roleOf(child))) return false;
    if (isBlockLevel(child)) return false;
  }
  return true;
}

function ownTextOf(el) {
  // Text nodes that are direct children of el, i.e. the runs that belong to no
  // descendant element.  Used for elements that are NOT text blocks: their
  // descendants' text is emitted separately, but these loose runs would
  // otherwise be lost, since querySelectorAll('*') returns elements only.
  let out = '';
  for (const node of Array.from(el.childNodes)) {
    if (node.nodeType === 3) out += node.nodeValue;
  }
  return out;
}

function headingLevelOf(el) {
  const tag = el.tagName.toLowerCase();
  if (/^h[1-6]$/.test(tag)) return Number(tag.slice(1));
  const aria = Number(el.getAttribute('aria-level'));
  return Number.isFinite(aria) && aria > 0 ? aria : 2;
}

function clean(s) {
  return String(s || '').replace(/\s+/g, ' ').trim().slice(0, 2000);
}

const out = [];
const root = selector === null ? document : document.querySelector(selector);
if (!root) throw new Error('selector matched no element');
const covered = new Set();
const __els = Array.from(root.querySelectorAll('*'));
const __style = new Map();
for (const el of __els) __style.set(el, window.getComputedStyle(el));
for (const el of __els) {
  const style = __style.get(el);
  if (style.visibility === 'hidden' || style.display === 'none') continue;
  // aria-hidden marks a subtree decorative: icon glyphs, duplicated mobile
  // nav, screen-reader spacers.  closest() covers the subtree, since the
  // attribute applies to every descendant in the accessibility tree.
  if (el.closest('[aria-hidden="true"]')) continue;
  const bbox = el.getBoundingClientRect();
  if (!bbox || bbox.width <= 0 || bbox.height <= 0) continue;
  let role = roleOf(el);
  // input[type=hidden] carries no box, but an explicit role="hidden" does.
  if (role === 'hidden') continue;
  // A generic element the page has wired for clicking is a control in every
  // way that matters to the agent, and needs a ref.  Three bounds, each of
  // which cost real over-promotion when it was missing:
  //   - generic only, so an onclick on a <section> cannot demote a landmark;
  //   - not already covered, i.e. not inside a control that was emitted
  //     earlier in document order.  Both cursor:pointer and the pointer
  //     cursor's inheritance make every <span> inside every <a> look
  //     clickable, which is what turned 13 Wikipedia buttons into 618;
  //   - tabindex >= 0.  A negative tabindex means focusable by script but
  //     deliberately NOT reachable by the user, which is the opposite of
  //     interactive, and it is how pages mark scroll targets and headings.
  if (role === 'generic' && !covered.has(el)) {
    const tabindex = Number(el.getAttribute('tabindex'));
    if (el.hasAttribute('onclick')
        || (el.hasAttribute('tabindex') && Number.isFinite(tabindex) && tabindex >= 0)
        || (style && style.cursor === 'pointer' && isTextBlock(el))) {
      role = 'button';
    }
  }
  const idx = base + out.length;
  el.setAttribute('data-sg-i', String(idx));
  let textBlock = '';
  if (__INTERACTIVE.has(role)) {
    // Text inside a control belongs to the control: nameOf already carries it
    // into the "- role @eN name" line.  Cover the subtree so a block-level
    // child (<a><div>Label</div></a>, ubiquitous in nav menus and card links)
    // cannot emit the same label a second time as a stray text line.
    for (const d of Array.from(el.querySelectorAll('*'))) covered.add(d);
  } else if (covered.has(el)) {
    // An ancestor text block already emitted this element's text.
    textBlock = '';
  } else if (isTextBlock(el)) {
    textBlock = clean(el.innerText || el.textContent);
    for (const d of Array.from(el.querySelectorAll('*'))) covered.add(d);
  } else {
    textBlock = clean(ownTextOf(el));
  }
  const entry = {
    role: role,
    name: nameOf(el, role),
    x: Math.round(bbox.x),
    y: Math.round(bbox.y),
    width: Math.round(bbox.width),
    height: Math.round(bbox.height),
    depth: depthOf(el),
    children_count: el.children ? el.children.length : 0,
    idx: idx,
    text_block: textBlock,
  };
  if (role === 'heading') entry.heading_level = headingLevelOf(el);
  out.push(entry);
}
return {
  viewport: {width: window.innerWidth, height: window.innerHeight},
  nodes: out,
};
}
