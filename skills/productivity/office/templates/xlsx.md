# Spreadsheet templates: a new workbook's first call

Start a new workbook from the template of its kind, in one call: it renames Sheet1, then writes the headers, the rows
and the totals, and sets the number formats and column widths. When no kind fits (a plain table, a sheet per month),
build from the closest one's parts, in the same header style, formats and formula totals. Put in the user's path and the
user's rows in place of the sample rows. The samples go in only for a user who gave no rows, and only where they fit the
request (each kind says what they are): say in the chat that they are samples. Rows the user asked for but did not
detail, and a request the samples do not fit, are drafted from common practice as a proposal: the usual rows, with the
figures, owners and dates left empty. Any other specific the user did not give (a name, an address, an invoice number, a
date, payment terms) stays empty, never made up. Ask only when the sheet cannot be made without the answer; say in the
chat what you drafted, for them to check, and what was missing.

Totals stay formulas: never type a number the sheet can work out, even one you worked out yourself. A list longer or
shorter than the sample ends at another row: every range over it (each SUM and COUNTIF, the formats, the chart's)
ends at that row too, and each row it gains gets its row's formulas. Write the user's currency in the number formats
in place of `$`: `€#,##0.00` for euros.

## Budget

For money in and out: a household's month, an event, a project. For a budget to spend, its income is the money
available: the allocation, a grant. The samples are a household's month.

```json
{"path": "budgets/monthly-budget.xlsx", "ops": [
  {"op": "sheet", "action": "rename", "name": "Sheet1", "to": "Budget"},
  {"op": "set", "range": "Budget!A1", "values": [["Summary", "Amount"], ["Total income", "=SUM(B7:B8)"], ["Total expenses", "=SUM(E7:E11)"], ["Left over", "=B2-B3"]]},
  {"op": "set", "range": "Budget!A6", "values": [["Income", "Amount"], ["Salary", 3000], ["Other income", 400]]},
  {"op": "set", "range": "Budget!D6", "values": [["Expenses", "Amount", "% of income"], ["Rent", 1100, "=E7/$B$2"], ["Food", 450, "=E8/$B$2"], ["Utilities", 200, "=E9/$B$2"], ["Transport", 150, "=E10/$B$2"], ["Savings", 400, "=E11/$B$2"]]},
  {"op": "format", "range": "Budget!A1:B1", "bold": true, "fill": "#14285A", "color": "#FFFFFF"},
  {"op": "format", "range": "Budget!A6:B6", "bold": true, "fill": "#14285A", "color": "#FFFFFF"},
  {"op": "format", "range": "Budget!D6:F6", "bold": true, "fill": "#14285A", "color": "#FFFFFF"},
  {"op": "format", "range": "Budget!A4:B4", "bold": true, "fill": "#DCE3F0"},
  {"op": "format", "range": "Budget!B2:B8", "number_format": "$#,##0.00"},
  {"op": "format", "range": "Budget!E7:E11", "number_format": "$#,##0.00"},
  {"op": "format", "range": "Budget!F7:F11", "number_format": "0%"},
  {"op": "format", "range": "Budget!A1", "width": 18},
  {"op": "format", "range": "Budget!B1", "width": 12},
  {"op": "format", "range": "Budget!C1", "width": 3},
  {"op": "format", "range": "Budget!D1", "width": 18},
  {"op": "format", "range": "Budget!E1:F1", "width": 12},
  {"op": "chart", "range": "Budget!D6:E11", "type": "pie", "title": "Expenses", "anchor": "H1"}
]}
```

Put the user's income in A7:B8 and the expenses in D7:E11, one per row, each expense with its row's % formula. The
summary in B2:B4 stays formulas. With n expenses the list ends at row 6+n: so do the SUM in B3, the formats over E and
F, and the chart's range. With n incomes, the SUM in B2 and the format over B end at row 6+n.

## Invoice

For a bill to one client: what was sold, the tax and the total due. The samples are web design work.

```json
{"path": "invoices/inv-0042.xlsx", "ops": [
  {"op": "sheet", "action": "rename", "name": "Sheet1", "to": "Invoice"},
  {"op": "set", "range": "Invoice!A1", "values": [["Invoice"], [""],
    ["From", "Harbour Lane Design, 4 Mill Road, Easton"], ["Bill to", "Eastside Walking Club, 18 Park Lane, Easton"],
    ["Invoice no.", "INV-0042"], ["Date", "2026-10-14"], ["Due date", "=B6+30"], [""],
    ["Subtotal", "", "", "=SUM(D15:D17)"], ["Tax", "", "20%", "=D9*C10"], ["Total due", "", "", "=D9+D10"],
    ["Payment within 30 days by bank transfer, quoting the invoice number."], [""],
    ["Description", "Quantity", "Unit price", "Amount"],
    ["Website design", 1, 1800, "=B15*C15"], ["Photography, half days", 2, 350, "=B16*C16"], ["Hosting, months", 12, 15, "=B17*C17"]]},
  {"op": "format", "range": "Invoice!A1:D1", "bold": true, "fill": "#14285A", "color": "#FFFFFF"},
  {"op": "format", "range": "Invoice!A3:A7", "bold": true},
  {"op": "format", "range": "Invoice!B6:B7", "number_format": "d mmm yyyy", "align": "left"},
  {"op": "format", "range": "Invoice!D9:D11", "number_format": "$#,##0.00"},
  {"op": "format", "range": "Invoice!C10", "number_format": "0%"},
  {"op": "format", "range": "Invoice!A11:D11", "bold": true, "fill": "#DCE3F0"},
  {"op": "format", "range": "Invoice!A12", "italic": true},
  {"op": "format", "range": "Invoice!A14:D14", "bold": true, "fill": "#14285A", "color": "#FFFFFF"},
  {"op": "format", "range": "Invoice!B14:D14", "align": "right"},
  {"op": "format", "range": "Invoice!C15:D17", "number_format": "$#,##0.00"},
  {"op": "format", "range": "Invoice!A1", "width": 28},
  {"op": "format", "range": "Invoice!B1", "width": 12},
  {"op": "format", "range": "Invoice!C1:D1", "width": 14}
]}
```

Write the user's seller, client, invoice number and date into B3:B6; a cell the user gave nothing for stays empty, never
made up. The due date in B7 and the payment line in A12 are filled only from the terms the user gave (their days in B7's
formula, their words in A12), else both stay empty; with no date in B6, B7 stays empty too. Then the items from row 15,
one per row, each Amount its row's Quantity times Unit price. With n items they end at row 14+n: so do the SUM in D9 and
the format over C and D. Subtotal, Tax and Total due stay formulas; the tax rate is C10 (0% for none).

## Tracker

For tasks with an owner, a due date and a status: a project plan, a to-do list, a launch. The samples are a website
launch.

```json
{"path": "plans/website-tracker.xlsx", "ops": [
  {"op": "sheet", "action": "rename", "name": "Sheet1", "to": "Tracker"},
  {"op": "set", "range": "Tracker!A1", "values": [["Task", "Owner", "Due", "Status", "Hours"],
    ["Agree the scope", "Ana", "2026-11-02", "Done", 6], ["Draft the page designs", "Ben", "2026-11-09", "Done", 16],
    ["Build the pages", "Chloe", "2026-11-23", "In progress", 40], ["Write the page copy", "Dan", "2026-11-23", "In progress", 12],
    ["Test with five users", "Ana", "2026-11-30", "Not started", 10], ["Launch the site", "Ben", "2026-12-07", "Not started", 4]]},
  {"op": "set", "range": "Tracker!G1", "values": [["Progress", ""], ["Not started", "=COUNTIF(D2:D7,G2)"],
    ["In progress", "=COUNTIF(D2:D7,G3)"], ["Done", "=COUNTIF(D2:D7,G4)"], ["% done", "=H4/COUNTA(A2:A7)"],
    ["Hours planned", "=SUM(E2:E7)"], ["Hours left", "=H6-SUMIF(D2:D7,G4,E2:E7)"]]},
  {"op": "format", "range": "Tracker!A1:E1", "bold": true, "fill": "#14285A", "color": "#FFFFFF"},
  {"op": "format", "range": "Tracker!G1:H1", "bold": true, "fill": "#14285A", "color": "#FFFFFF"},
  {"op": "format", "range": "Tracker!C2:C7", "number_format": "d mmm yyyy"},
  {"op": "format", "range": "Tracker!H5", "number_format": "0%"},
  {"op": "format", "range": "Tracker!A1", "width": 28},
  {"op": "format", "range": "Tracker!B1:D1", "width": 13},
  {"op": "format", "range": "Tracker!F1", "width": 3},
  {"op": "format", "range": "Tracker!G1", "width": 15}
]}
```

Put the user's tasks in rows 2 to 7, one per row, each due date as 2026-11-02. Status is Not started, In progress or
Done, spelled as in G2:G4: the counts look for those words. H2:H7 stay formulas. With n tasks the list ends at row
n+1: so does every range in H2:H7 and the date format over C.
