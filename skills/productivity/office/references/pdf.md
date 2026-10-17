# PDFs (.pdf)

## Reading

- `office_read` view `outline`: every page with its size in points, its rotation, its first line, its form fields
  (name, type, value, rect; a checkbox's value is true or false, as `fill` takes it) and annotations. A long PDF comes
  in parts: continue with `start`.
- view `page` with `target: <page number>`: the page's text.
- view `search` with `query`: the pages that hold the words.

Rects are `[x1, y1, x2, y2]` in points (1/72 inch) **from the page's top-left corner**, y growing down: a field 20 pt
tall, 60 pt below the top of an A4 page, is `[x, 60, x + width, 80]`.

## Editing

A call's ops apply together or not at all, and each finds the document as the ops before it left it (a page added by
an earlier op counts).

```json
{"op": "fill", "fields": {"client": "ACME Lisbon Lda", "signed": true}, "expect": {"client": null, "signed": false}}
```

```json
{"op": "add_field", "type": "text", "page": 1, "rect": [170, 250, 420, 270], "name": "po_number", "value": "PO-4471"}
```

```json
{"op": "pages", "action": "add", "page": 2}
{"op": "pages", "action": "rotate", "page": 3, "degrees": 90}
{"op": "pages", "action": "remove", "page": 4}
```

```json
{"ops": [{"op": "redact", "text": "account number 4471", "match_case": false}]}
```

Place a note by the words it is about: `anchor` quotes them, and the note goes beside them, clear of the page's text,
fields and notes. The result gives the `page` and `rect` it landed at.

```json
{"op": "annotate", "kind": "free_text", "anchor": {"find": "thirty days"}, "text": "Net 30 from the invoice date."}
{"op": "annotate", "kind": "stamp", "anchor": {"find": "Signed:", "nth": 1}, "stamp": "approved"}
```

For a place no words mark, give `page` and `rect` instead:

```json
{"op": "annotate", "kind": "free_text", "page": 1, "rect": [60, 560, 280, 600], "text": "Checked against the order."}
{"op": "annotate", "kind": "stamp", "page": 1, "rect": [300, 360, 450, 410], "stamp": "approved"}
```

- `fill`: text for a text field, true or false for a checkbox; the names are the outline's. Pass `expect`: each field's
  value as the outline showed it (null when empty). A field someone changed since your read refuses the call: read
  again and work from its value now.
- `pages`: `add` puts a blank page after `page` (0 for the first); `rotate` turns a page to 0, 90, 180 or 270 degrees.
- `redact` removes every occurrence of the words. It goes in a call of its own: one redaction, nothing beside it.
  **Once the document saves, they are gone for good.** If someone has redaction marks of their own pending, the call is
  refused: ask the user to apply or remove them first.
- `annotate`: a `free_text` note, or a `stamp`: approved, not_approved, draft, final, confidential, for_comment,
  information_only, completed, void, sign_here. It takes `anchor` or `page` and `rect`, not both. `anchor.nth` picks a
  later match (from 1, through the pages in order); `match_case` is false unless you set it. With no free place beside
  the words, the call is refused: give `page` and `rect`.

**Existing text cannot be changed.** To correct a PDF's words, tell the user; offer a note, a redaction or a new page.

## Comments

A comment in a PDF is an annotation, usually a sticky note (`text`), and its replies. The outline lists each page's
annotations with their `id`, author, text, latest replies and whether they are resolved. Reply and resolve in a call of
their own:

```json
{"ops": [{"op": "reply", "thread": "1_1", "body": "March: the contract says 1 March 2026."}, {"op": "resolve", "thread": "1_1"}]}
```

A reply states only the result, in one or two sentences (at most 400 characters). Resolve last.
