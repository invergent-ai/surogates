# Spreadsheets (.xlsx)

## Reading

- `office_read` view `outline`: every sheet with its used range, its first two rows as shown, merged cells and charts
  (name, type, title, data range, anchor). Protected sheets and a protected workbook say so.
- view `range` with `target: "Sales!A1:D20"`: the cells **as shown** (`values`), the formulas of the cells that have
  one, and the number formats that are not General. A long range comes in parts: view its `next` as the next `target`.
- view `search` with `query`: every cell whose shown text holds the words, as `Sheet!A1`.

A range always names its sheet: `Sales!A1:D20`, `'Q3 plan'!B2`.

## Writing

`set` writes a block of cells. Give its top-left cell, or the whole range, and the rows of values. Each value goes in
exactly as typed into the cell:

- `"=SUM(B2:B5)"` is a live formula; `120` and `"120"` are numbers; `"2026-03-01"` is a date; `"50%"` is a percent.
- A leading `'` keeps text as text: `"'0042"` stays `0042`.
- `""` or `null` empties a cell.

Pass `expect`: the range's `values` exactly as `office_read` showed them. If anyone changed a cell since, nothing
applies and the result shows the range as it is now.

```json
{"op": "set", "range": "Sales!A6", "values": [["West", 50, 60, "=B6+C6"]], "expect": [["", "", "", ""]]}
```

```json
{"op": "set", "range": "Sales!A7:D7", "values": [["Total", "=SUM(B2:B6)", "=SUM(C2:C6)", "=SUM(D2:D6)"]]}
```

`format` styles a range: `number_format` (an Excel code), `bold`, `italic`, `wrap`, `fill` (`"#RRGGBB"` or `"none"`),
`color`, `align` (left, center, right, justify), `valign` (top, center, bottom), `width` (column width in characters).

```json
{"op": "format", "range": "Sales!A1:D1", "bold": true, "fill": "#14285A", "color": "#FFFFFF"}
```

```json
{"op": "format", "range": "Sales!B2:D7", "number_format": "#,##0", "align": "right", "width": 12}
```

`chart` adds a chart from a range whose first row names the series and whose first column holds the categories. Its
`type` is column, bar, line, pie, area, doughnut or scatter; `anchor` is the cell its top-left corner sits on. The
result gives its name.

```json
{"op": "chart", "range": "Sales!A1:C5", "type": "column", "title": "Q1 and Q2 by region", "anchor": "F2"}
```

`sheet` adds (after a named sheet, or last), renames (`to` the new name) or deletes a sheet. A call changes each sheet
at most once, and sheet changes apply before the call's other ops, so a sheet added in a call can be written in the same
call.

```json
{"ops": [{"op": "sheet", "action": "add", "name": "Plan", "after": "Sales"}, {"op": "set", "range": "Plan!A1", "values": [["Hire two engineers"]]}]}
```

## Refusals

- **Someone is editing a cell you write:** the call names them and the cell. Try again in a moment, or ask them.
- **A protected sheet or workbook:** only the user can lift the protection.
