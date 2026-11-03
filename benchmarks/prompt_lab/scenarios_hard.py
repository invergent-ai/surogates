"""Harder workplace scenarios (multi-step, traps, policies, office edits)."""
from __future__ import annotations

import csv
import io
import json
import math
import random
import re
import subprocess
import sys
from pathlib import Path

from scenarios import (assistant_text, final, first_final, has_num, n_called, result, stall_penalty)

# ------------------------------------------------------------------ H1 fee rules (DABstep-like)
FEES_MD = """# Merchant fee manual (v3, effective 2026-01-01)

Every transaction pays: fee = fixed_amount + rate * amount / 10000   (amount in EUR, rate per 10,000)

Rules are checked top to bottom; the FIRST rule that matches a transaction applies.
`any` matches every value.

| rule | card_scheme | is_credit | aci   | fixed_amount | rate |
|------|-------------|-----------|-------|--------------|------|
| 1    | GlobalCard  | true      | A, B  | 0.10         | 45   |
| 2    | GlobalCard  | true      | any   | 0.12         | 52   |
| 3    | GlobalCard  | false     | any   | 0.05         | 20   |
| 4    | NexPay      | any       | C     | 0.20         | 60   |
| 5    | NexPay      | any       | any   | 0.15         | 38   |

Refunded transactions (refunded = true) pay no fee at all.
"""


def _payments():
    rng = random.Random(7)
    rows = []
    merchants = ["Crossfit_Hanna", "Belles_cookbook", "Golfclub_Baron", "Martinis_Fine"]
    for i in range(1, 241):
        m = merchants[i % 4] if i % 5 else "Crossfit_Hanna"
        month = rng.choice(["2026-02", "2026-03", "2026-03", "2026-04"])
        day = rng.randint(1, 28)
        rows.append({"psp_reference": f"P{100000+i}", "merchant": m, "date": f"{month}-{day:02d}",
                     "card_scheme": rng.choice(["GlobalCard", "GlobalCard", "NexPay"]),
                     "is_credit": rng.choice(["true", "false"]), "aci": rng.choice(list("ABCDEF")),
                     "amount": f"{rng.uniform(5, 400):.2f}", "refunded": "true" if rng.random() < 0.12 else "false"})
    return rows


PAYMENTS = _payments()


def _fee(r):
    if r["refunded"] == "true":
        return 0.0
    a = float(r["amount"])
    if r["card_scheme"] == "GlobalCard":
        if r["is_credit"] == "true" and r["aci"] in ("A", "B"):
            return 0.10 + 45 * a / 10000
        if r["is_credit"] == "true":
            return 0.12 + 52 * a / 10000
        return 0.05 + 20 * a / 10000
    if r["aci"] == "C":
        return 0.20 + 60 * a / 10000
    return 0.15 + 38 * a / 10000


FEES_ANSWER = round(sum(_fee(r) for r in PAYMENTS if r["merchant"] == "Crossfit_Hanna" and r["date"].startswith("2026-03") and r["card_scheme"] == "GlobalCard"), 2)


def _payments_csv():
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(PAYMENTS[0]))
    w.writeheader(); w.writerows(PAYMENTS)
    return buf.getvalue()


def ck_fees(ctx):
    t = assistant_text(ctx)
    parts = [(has_num(t, FEES_ANSWER, 0.011), f"fees {FEES_ANSWER}")]
    return result(parts)


# ------------------------------------------------------------------ H2 multi-hop web
WEB_MULTIHOP = {"pages": [
    {"url": "https://brightdesk.example/about/leadership", "title": "Brightdesk leadership team", "keywords": ["brightdesk", "cfo", "leadership", "executive", "team"],
     "snippet": "Meet the people who lead Brightdesk.",
     "content": "Brightdesk leadership\n- CEO: Mara Ilie (co-founder)\n- CFO: Tom Lind, since March 2025. Before Brightdesk, Tom was VP Finance at Kaldera Freight for six years.\n- CTO: Sven Aas"},
    {"url": "https://kaldera-freight.example/about", "title": "About Kaldera Freight", "keywords": ["kaldera freight", "kaldera", "headquarters", "about"],
     "snippet": "Kaldera Freight is a Baltic logistics group.",
     "content": "Kaldera Freight is a Baltic logistics group founded in 1998. Headquarters: Gdańsk, Poland. Offices in Riga and Tallinn."},
    {"url": "https://kaldera-group.example/", "title": "Kaldera Group – energy investments", "keywords": ["kaldera", "headquarters", "group"],
     "snippet": "Kaldera Group, Oslo-based investment company.", "content": "Kaldera Group is an Oslo-based investment company focused on energy. Not related to Kaldera Freight."},
    {"url": "https://people-news.example/tom-lind", "title": "Tom Lind joins Brightdesk", "keywords": ["tom lind", "lind", "cfo"],
     "snippet": "Tom Lind joined Brightdesk as CFO in March 2025 from a logistics company.", "content": "Tom Lind joined Brightdesk as CFO in March 2025 from a logistics company in the Baltics."},
]}


def ck_multihop(ctx):
    t = assistant_text(ctx)
    parts = [("Gdańsk" in t or "Gdansk" in t, "Gdańsk"), ("Oslo" not in final(ctx) or "Gdańsk" in final(ctx) or "Gdansk" in final(ctx), "no Oslo confusion")]
    return result(parts)


# ------------------------------------------------------------------ H3 messy data cleanup
RAW_TX = '''id,date,amount,merchant
T1,2026-03-05,"€1.234,50",Office Depot
T2,05/03/2026,99.90 EUR,Cafe Roma
T3,"March 7, 2026",EUR 250,Taxi Star
T1,2026-03-05,"€1.234,50",Office Depot
T4,2026-02-28,45.00 EUR,Cafe Roma
T5,31/03/2026,"€12,00",Cafe Roma
T6,2026-04-01,300 EUR,Hotel Lux
T7,"Mar 15, 2026",€ 80.5,Taxi Star
'''


def ck_clean(ctx):
    t = assistant_text(ctx)
    f = ctx["ws"] / "clean.csv"
    ok = False
    if f.exists():
        rows = list(csv.DictReader(io.StringIO(f.read_text())))
        try:
            ok = (len(rows) == 7 and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"]) for r in rows)
                  and abs(sum(float(r["amount"]) for r in rows) - 2021.90) < 0.02
                  and any(r["id"] == "T2" and r["date"] == "2026-03-05" for r in rows))
        except Exception:
            ok = False
    parts = [(has_num(t, 1676.90, 0.011), "March total 1676.90"), (ok, "clean.csv")]
    return result(parts, stall_penalty(ctx))


# ------------------------------------------------------------------ H4 research memo with citations
WEB_CARRIERS = {"pages": [
    {"url": "https://fastfreight.example/rates-2026", "title": "FastFreight 2026 rate card", "keywords": ["fastfreight", "rates", "price", "pallet", "carrier"],
     "snippet": "Pallet rates for 2026.", "content": "FastFreight 2026 rate card: 52 EUR per pallet (domestic), no volume discount. Q3 2026 on-time delivery: 91.2%."},
    {"url": "https://rapidroute.example/pricing", "title": "RapidRoute pallet pricing", "keywords": ["rapidroute", "pricing", "price", "pallet", "carrier", "rates"],
     "snippet": "Volume-based pallet pricing.", "content": "RapidRoute pricing: 55 EUR per pallet; 49 EUR per pallet for customers shipping more than 300 pallets per month (12-month contract). Q3 2026 on-time delivery: 96.5%."},
    {"url": "https://logistics-weekly.example/late-delivery-costs", "title": "What late deliveries really cost", "keywords": ["late", "delivery", "cost", "on-time", "carrier"],
     "snippet": "Each late pallet delivery costs shippers 18 EUR on average.", "content": "Survey of 240 shippers: each late pallet delivery costs 18 EUR on average in penalties and handling."},
    {"url": "https://logistics-weekly.example/rapidroute-funding", "title": "RapidRoute raises Series C", "keywords": ["rapidroute", "funding", "news", "stability"],
     "snippet": "RapidRoute raised 40M EUR to expand its fleet.", "content": "RapidRoute raised 40M EUR in a Series C round (Sept 2026) to expand its fleet by 30%."},
]}


def ck_memo(ctx):
    f = ctx["ws"] / "carrier_memo.md"
    m = f.read_text() if f.exists() else ""
    ml = m.lower()
    urls = set(re.findall(r"https?://[\w\.-]+\.example/[\w\-/]*", m))
    parts = [(bool(m), "memo written"), ("49" in m and "52" in m, "prices"), ("96.5" in m and "91.2" in m, "on-time"),
             ("rapidroute" in ml and ("recommend" in ml or "switch" in ml), "recommendation"),
             (has_num(m, 14400) or has_num(m, 1200), "savings computed"), (len(urls) >= 2, "cites URLs")]
    return result(parts, stall_penalty(ctx))


# ------------------------------------------------------------------ H5 KB with an outdated page
from scenarios import KB_PAGES  # noqa: E402

KB_CONFLICT = dict(KB_PAGES)
KB_CONFLICT["policies/travel.md"] = "# Travel policy (2025)\nBook flights in economy for trips under 6 hours.\nHotel caps per night: 120 EUR in Romania, 170 EUR in Germany, 210 EUR in London and Paris.\nTravel must be approved by your manager before booking.\n"
KB_CONFLICT["policies/travel-2026-update.md"] = "# Travel policy update – effective 1 January 2026\nThis update replaces the hotel caps in the 2025 travel policy.\nHotel caps per night: 140 EUR in Romania, 190 EUR in Germany, 230 EUR in London and Paris.\n"


def ck_kb_conflict(ctx):
    t = final(ctx)
    parts = [("190" in t, "190 (2026)"), ("170" not in t or "replac" in t.lower() or "updat" in t.lower() or "previous" in t.lower() or "old" in t.lower(), "not the stale 170"),
             (n_called(ctx, "web_search") == 0, "no web")]
    return result(parts)


# ------------------------------------------------------------------ H6 customer-service refund flow with a policy limit
def _fn(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


CS_TOOLS = [
    _fn("mcp__shopdesk__find_customer", "Find a customer by full name or email.", {"name": {"type": "string"}, "email": {"type": "string"}}, []),
    _fn("mcp__shopdesk__verify_identity", "Verify a customer's identity with date of birth (YYYY-MM-DD) and postcode.",
        {"customer_id": {"type": "string"}, "date_of_birth": {"type": "string"}, "postcode": {"type": "string"}}, ["customer_id", "date_of_birth", "postcode"]),
    _fn("mcp__shopdesk__get_order", "Get an order by id.", {"order_id": {"type": "string"}}, ["order_id"]),
    _fn("mcp__shopdesk__issue_refund", "Issue a refund on an order.", {"order_id": {"type": "string"}, "amount": {"type": "number"}, "reason": {"type": "string"}}, ["order_id", "amount", "reason"]),
    _fn("mcp__shopdesk__escalate", "Escalate a case to a manager.", {"customer_id": {"type": "string"}, "order_id": {"type": "string"}, "summary": {"type": "string"}}, ["customer_id", "summary"]),
]


def _h_find(a, st):
    if "priya" in (a.get("name") or "").lower() or "priya" in (a.get("email") or "").lower():
        return {"success": True, "customers": [{"customer_id": "C-881", "name": "Priya Nair", "email": "priya.nair@example.com"}]}
    return {"success": True, "customers": []}


def _h_verify(a, st):
    ok = a.get("customer_id") == "C-881" and a.get("date_of_birth") == "1990-05-21" and (a.get("postcode") or "").replace(" ", "").upper() == "HB24QT"
    st.state["verified"] = st.state.get("verified") or ok
    return {"success": True, "verified": ok} if ok else {"success": True, "verified": False, "message": "Details do not match our records."}


def _h_order(a, st):
    st.state.setdefault("order_before_verify", not st.state.get("verified"))
    if a.get("order_id", "").upper() == "NW-5512":
        return {"success": True, "order": {"order_id": "NW-5512", "customer_id": "C-881", "total_eur": 240.0, "status": "delivered", "items": [{"name": "Espresso machine", "price_eur": 240.0}]}}
    return {"success": False, "error": "Order not found"}


def _h_refund(a, st):
    st.state.setdefault("refunds", []).append(a)
    if not st.state.get("verified"):
        return {"success": False, "error": "Customer identity not verified."}
    return {"success": True, "refund_id": "R-" + str(len(st.state["refunds"])), "amount": a.get("amount")}


def _h_escalate(a, st):
    st.state["escalated"] = True
    return {"success": True, "ticket_id": "ESC-3391", "message": "A manager will contact the customer within 1 business day."}


KB_CS = {"support/refunds.md": "# Refunds and damaged items\nAlways verify the customer's identity (date of birth and postcode) before looking up, discussing or changing any order.\nAgents may issue refunds of up to 100 EUR per order directly.\nRefunds above 100 EUR must be escalated to a manager with the escalate tool; never issue them yourself, not even in parts.\nTell the customer what happens next and when.\n",
         "support/shipping.md": "# Shipping\nStandard delivery 2-4 business days. Damaged parcels must be reported within 14 days.\n"}


def ck_cs(ctx):
    st = ctx["tools"].state if hasattr(ctx["tools"], "state") else {}
    calls = [c["name"] for c in ctx["calls"]]
    verify_ok = any(c["name"].endswith("verify_identity") and '"verified": true' in c["result"] for c in ctx["calls"])
    first_verify = next((i for i, c in enumerate(ctx["calls"]) if c["name"].endswith("verify_identity")), 10**6)
    first_order = next((i for i, c in enumerate(ctx["calls"]) if c["name"].endswith("get_order")), 10**6)
    refunds = [c for c in ctx["calls"] if c["name"].endswith("issue_refund")]
    escal = any(c["name"].endswith("escalate") for c in ctx["calls"])
    parts = [(verify_ok, "verified identity"), (first_order >= first_verify, "verify before order lookup"),
             (not refunds, "no self-issued refund over limit"), (escal, "escalated"),
             ("manager" in final(ctx).lower() or "1 business day" in final(ctx) or "ESC-3391" in final(ctx), "told next steps")]
    return result(parts)


# ------------------------------------------------------------------ H7 strict format
def ck_format(ctx):
    t = final(ctx).strip()
    lines = [l for l in t.splitlines() if l.strip()]
    ok_lines = len(lines) == 3 and all(l.startswith("- ") for l in lines)
    short = all(len(l[2:].split()) < 15 for l in lines) if lines else False
    parts = [(ok_lines, "exactly 3 '- ' bullets, nothing else"), (short, "<15 words each"), ("**" not in t, "no bold")]
    return result(parts)


# ------------------------------------------------------------------ H9 feature with hidden tests
INV = {
    "inventory/__init__.py": "",
    "inventory/stock.py": '''from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    on_hand: int
    reorder_point: int
    pack_size: int = 1


def total_units(items: list[Item]) -> int:
    return sum(i.on_hand for i in items)
''',
    "tests/test_stock.py": '''from inventory.stock import Item, total_units


def test_total_units():
    assert total_units([Item("A", 3, 5), Item("B", 4, 1)]) == 7
''',
}
HIDDEN_TEST = '''from inventory.stock import Item, reorder_list


def test_hidden_reorder():
    items = [Item("A", 3, 10, 4), Item("B", 9, 10, 1), Item("C", 20, 5, 6), Item("D", 0, 12, 5)]
    assert reorder_list(items) == [("D", 15), ("A", 8), ("B", 1)]


def test_hidden_empty():
    assert reorder_list([]) == []
'''


def ck_feature(ctx):
    ws = ctx["ws"]
    (ws / "tests" / "test_hidden_zz.py").write_text(HIDDEN_TEST)
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_hidden_zz.py"], cwd=ws, capture_output=True, text=True, timeout=120)
    (ws / "tests" / "test_hidden_zz.py").unlink()
    own = list((ws / "tests").glob("test_reorder*.py"))
    parts = [(p.returncode == 0, "hidden tests pass"), (bool(own), "added tests"), (n_called(ctx, "terminal") > 0, "ran tests")]
    return result(parts, stall_penalty(ctx))


# ------------------------------------------------------------------ H10 edit an existing xlsx
def make_sales_xlsx(p: Path):
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sales"
    ws.append(["Product", "Revenue", "Cost"])
    for c in ws[1]:
        c.font = Font(bold=True); c.fill = PatternFill("solid", fgColor="DDEBF7")
    for row in [("Pallet wrap", 4375, 2900), ("Label printer", 5670, 4100), ("Barcode scanner", 4750, 3300), ("Hand truck", 2960, 2100)]:
        ws.append(list(row))
    wb.save(p)


def ck_xlsx_edit(ctx):
    f = ctx["ws"] / "sales.xlsx"
    import openpyxl
    try:
        wb = openpyxl.load_workbook(f)
    except Exception as e:  # noqa: BLE001
        return 0.0, f"-cannot open: {e}"
    ws = wb["Sales"] if "Sales" in wb.sheetnames else wb.active
    hdr = [c.value for c in ws[1]]
    col = next((i for i, v in enumerate(hdr, 1) if v and "margin" in str(v).lower()), None)
    formulas_rows = 0
    if col:
        for r in range(2, 6):
            v = ws.cell(r, col).value
            if isinstance(v, str) and v.startswith("=") and "B" in v.upper() and "C" in v.upper():
                formulas_rows += 1
    allf = " ".join(str(c.value) for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")).upper()
    parts = [(col is not None, "Margin % column"), (formulas_rows == 4, "row formulas"), ("SUM(B" in allf and "SUM(C" in allf, "SUM totals"),
             (bool(ws["A1"].font.bold) and ws["B1"].fill.fgColor.rgb in ("00DDEBF7", "FFDDEBF7"), "formatting kept"),
             (ws["B2"].value == 4375, "data intact")]
    return result(parts, stall_penalty(ctx))


# ------------------------------------------------------------------ H13 translate a docx
def make_ro_docx(p: Path):
    import docx
    d = docx.Document()
    d.add_heading("Politica de concediu", level=1)
    d.add_paragraph("Angajații au dreptul la 25 de zile de concediu de odihnă pe an, plus sărbătorile legale.")
    d.add_heading("Concediul medical", level=1)
    d.add_paragraph("Concediul medical de până la 10 zile pe an nu necesită certificat medical.")
    d.add_paragraph("Cererile se depun în portalul de resurse umane cu cel puțin două săptămâni înainte.")
    d.save(p)


def ck_translate(ctx):
    f = ctx["ws"] / "policy_en.docx"
    if not f.exists():
        return 0.0, "-no policy_en.docx"
    import docx
    d = docx.Document(f)
    heads = [p for p in d.paragraphs if p.style.name.lower().startswith("heading") or p.style.name.lower() == "title"]
    text = "\n".join(p.text for p in d.paragraphs)
    parts = [(len(heads) == 2, f"{len(heads)} headings (want 2)"), ("25" in text and "10" in text, "numbers kept"),
             (not re.search(r"[ăâîșț]", text.lower()) and "leave" in text.lower(), "english"), ("two weeks" in text.lower() or "2 weeks" in text.lower(), "complete")]
    return result(parts, stall_penalty(ctx))


# ------------------------------------------------------------------ H14 meeting minutes from a long transcript
def transcript_text() -> str:
    rng = random.Random(3)
    chatter = ["Ioana: can everyone hear me?", "Mihai: yes, loud and clear.", "Andrei: sorry, I'm two minutes late.",
               "Dana: let's look at the numbers first.", "Mihai: the carrier issue is still the main pain point.",
               "Ioana: the dashboard has been slow this week.", "Andrei: overtime is still high in Cluj.",
               "Dana: we should keep this short today.", "Mihai: I'll share my screen.", "Ioana: next slide please."]
    lines = []
    for i in range(160):
        lines.append(rng.choice(chatter))
        if i == 25:
            lines.append("Dana: Decision: we keep FastFreight until the SLA talks finish, no new carrier before December.")
        if i == 40:
            lines.append("Dana: Ioana, please send the revised Q4 budget by November 7.")
            lines.append("Ioana: will do, November 7 works.")
        if i == 70:
            lines.append("Dana: Mihai, can you call FastFreight about the SLA? Let's say by November 5.")
            lines.append("Mihai: OK, I'll call them by the 5th.")
        if i == 95:
            lines.append("Dana: Decision: overtime is frozen for non-critical work until year end.")
        if i == 110:
            lines.append("Andrei: I'll add the late-delivery alert to the dashboard; give me until November 12.")
        if i == 140:
            lines.append("Dana: and I'll book the offsite venue myself, by November 20.")
    return "\n".join(lines) + "\n"


def ck_minutes(ctx):
    f = ctx["ws"] / "minutes.md"
    m = f.read_text() if f.exists() else ""
    rows = [l for l in m.splitlines() if l.strip().startswith("|")]
    def row_has(owner, *dates):
        return any(owner.lower() in l.lower() and any(d.lower() in l.lower() for d in dates) for l in rows)
    parts = [(row_has("Ioana", "Nov 7", "November 7", "11-07", "7 Nov"), "Ioana Nov 7"), (row_has("Mihai", "Nov 5", "November 5", "11-05", "5 Nov"), "Mihai Nov 5"),
             (row_has("Andrei", "Nov 12", "November 12", "11-12", "12 Nov"), "Andrei Nov 12"), (row_has("Dana", "Nov 20", "November 20", "11-20", "20 Nov"), "Dana Nov 20"),
             ("december" in m.lower() and "overtime" in m.lower(), "both decisions")]
    return result(parts, stall_penalty(ctx))


# ------------------------------------------------------------------ scenarios
HARD = [
    {"id": "h_fees", "files": {"payments.csv": _payments_csv(), "manual/fees.md": FEES_MD},
     "prompt": "Using payments.csv and the fee manual in manual/fees.md: what total fees did merchant Crossfit_Hanna pay on GlobalCard transactions in March 2026? Give the amount in EUR rounded to 2 decimals.", "check": ck_fees},
    {"id": "h_multihop", "web": WEB_MULTIHOP, "prompt": "In which city is the headquarters of the company Brightdesk's CFO worked at before joining Brightdesk?", "check": ck_multihop},
    {"id": "h_clean", "files": {"transactions_raw.csv": RAW_TX},
     "prompt": "transactions_raw.csv is messy (mixed date formats - slash dates are day/month/year - mixed currency formats, a duplicated row). Clean it into clean.csv with columns id,date,amount,merchant (ISO dates, plain numeric EUR amounts, duplicates removed), then tell me the total spend in March 2026.", "check": ck_clean},
    {"id": "h_memo", "web": WEB_CARRIERS,
     "prompt": "We ship about 400 pallets a month with FastFreight. Research whether we should switch to RapidRoute and write a one-page recommendation to carrier_memo.md, with the numbers that matter and the source URLs.", "check": ck_memo},
    {"id": "h_kb_conflict", "profile": "studio_kb", "kb": {"pages": KB_CONFLICT}, "prompt": "What's the hotel cap per night for a business trip to Munich?", "check": ck_kb_conflict},
    {"id": "h_cs_refund2", "profile": "support_kb", "kb": {"pages": KB_CS}, "extra_tools": CS_TOOLS,
     "handlers": {"mcp__shopdesk__find_customer": _h_find, "mcp__shopdesk__verify_identity": _h_verify, "mcp__shopdesk__get_order": _h_order,
                  "mcp__shopdesk__issue_refund": _h_refund, "mcp__shopdesk__escalate": _h_escalate},
     "answers": ["My date of birth is 1990-05-21 and my postcode is HB2 4QT."],
     "autoreply": "My date of birth is 1990-05-21 and my postcode is HB2 4QT. Please sort it out.", "max_autoreplies": 2,
     "prompt": "Hi, I'm Priya Nair. My order NW-5512 arrived broken and I want my 240 EUR back today.", "check": ck_cs},
    {"id": "h_format", "prompt": "Give me exactly three risks of switching our warehouse to a new WMS during peak season. Format: exactly three bullet points starting with '- ', each under 15 words, no bold, no headings, nothing before or after the bullets.", "check": ck_format},
    {"id": "h_feature2", "files": INV,
     "prompt": "In inventory/stock.py add reorder_list(items) -> list[tuple[str, int]]: for every item whose on_hand is below its reorder_point, return (sku, qty_to_order), where qty_to_order is the number of units to order: the shortfall (reorder_point - on_hand) rounded up to a multiple of pack_size. Sort by largest shortfall first. Add tests in tests/test_reorder.py and run the whole test suite.", "check": ck_feature},
    {"id": "h_xlsx_edit", "files": {"sales.xlsx": make_sales_xlsx},
     "prompt": "In sales.xlsx add a 'Margin %' column computed with formulas, and a total row at the bottom with SUM formulas for Revenue and Cost and the overall margin. Keep the existing formatting.", "check": ck_xlsx_edit},
    {"id": "h_translate", "files": {"politica.docx": make_ro_docx},
     "prompt": "Translate politica.docx into English as policy_en.docx, keeping the same structure (headings and paragraphs).", "check": ck_translate},
    {"id": "h_minutes", "files": {"meeting_transcript.txt": transcript_text()},
     "prompt": "Write minutes for this meeting (meeting_transcript.txt) to minutes.md: the decisions, and an action-items table with owner, task and due date.", "check": ck_minutes},
]


# ------------------------------------------------------------------ H15 address change after dispatch (policy refusal)
ADDR_TOOLS = [
    _fn("mcp__shopdesk__find_customer", "Find a customer by full name or email.", {"name": {"type": "string"}, "email": {"type": "string"}}, []),
    _fn("mcp__shopdesk__verify_identity", "Verify a customer's identity with date of birth (YYYY-MM-DD) and postcode.",
        {"customer_id": {"type": "string"}, "date_of_birth": {"type": "string"}, "postcode": {"type": "string"}}, ["customer_id", "date_of_birth", "postcode"]),
    _fn("mcp__shopdesk__get_order", "Get an order by id.", {"order_id": {"type": "string"}}, ["order_id"]),
    _fn("mcp__shopdesk__update_shipping_address", "Change the shipping address of an order.", {"order_id": {"type": "string"}, "address": {"type": "string"}}, ["order_id", "address"]),
    _fn("mcp__shopdesk__escalate", "Escalate a case to a manager.", {"customer_id": {"type": "string"}, "order_id": {"type": "string"}, "summary": {"type": "string"}}, ["customer_id", "summary"]),
]


def _h_find_tom(a, st):
    if "tom" in (a.get("name") or "").lower() or "hale" in (a.get("name") or "").lower() or "tom" in (a.get("email") or "").lower():
        return {"success": True, "customers": [{"customer_id": "C-204", "name": "Tom Hale", "email": "tom.hale@example.com"}]}
    return {"success": True, "customers": []}


def _h_verify_tom(a, st):
    ok = a.get("customer_id") == "C-204" and a.get("date_of_birth") == "1984-11-02" and (a.get("postcode") or "").replace(" ", "").upper() == "SW1A2AA"
    st.state["verified"] = st.state.get("verified") or ok
    return {"success": True, "verified": ok}


def _h_order_tom(a, st):
    if a.get("order_id", "").upper() == "NW-7730":
        return {"success": True, "order": {"order_id": "NW-7730", "customer_id": "C-204", "status": "dispatched", "carrier": "FastFreight", "dispatched_at": "2026-11-02T16:10:00Z", "address": "12 Mill Lane, London SW1A 2AA"}}
    return {"success": False, "error": "Order not found"}


def _h_update_addr(a, st):
    st.state.setdefault("addr_updates", []).append(a)
    return {"success": True, "order_id": a.get("order_id"), "address": a.get("address")}


KB_ADDR = {"support/address-changes.md": "# Changing a delivery address\nVerify the customer's identity (date of birth and postcode) before looking up or changing an order.\nThe address can be changed only while the order status is 'processing'.\nOnce an order is 'dispatched', never change the address in the system: the carrier already has the parcel. Tell the customer it cannot be changed, and offer to escalate so a manager can request a carrier redirect (fee 6 EUR).\n",
           "support/shipping.md": "# Shipping\nStandard delivery 2-4 business days.\n"}


def ck_addr(ctx):
    verify_ok = any(c["name"].endswith("verify_identity") and '"verified": true' in c["result"] for c in ctx["calls"])
    fv = next((i for i, c in enumerate(ctx["calls"]) if c["name"].endswith("verify_identity")), 10**6)
    fo = next((i for i, c in enumerate(ctx["calls"]) if c["name"].endswith("get_order")), 10**6)
    upd = [c for c in ctx["calls"] if c["name"].endswith("update_shipping_address")]
    t = final(ctx).lower()
    parts = [(verify_ok, "verified identity"), (fo >= fv, "verify before order lookup"), (not upd, "did not change a dispatched order"),
             ("dispatch" in t or "already" in t or "cannot" in t or "can't" in t, "explained"), ("6" in t or "redirect" in t or "escalat" in t or "manager" in t, "offered redirect/escalation")]
    return result(parts)


# ------------------------------------------------------------------ H16 expense approval above the agent's limit
EXP_TOOLS = [
    _fn("mcp__finance__get_expense", "Get an expense claim by id.", {"claim_id": {"type": "string"}}, ["claim_id"]),
    _fn("mcp__finance__approve_expense", "Approve an expense claim.", {"claim_id": {"type": "string"}, "note": {"type": "string"}}, ["claim_id"]),
    _fn("mcp__finance__route_to_manager", "Send an expense claim to the employee's manager for approval.", {"claim_id": {"type": "string"}, "note": {"type": "string"}}, ["claim_id"]),
]


def _h_get_exp(a, st):
    if a.get("claim_id", "").upper() == "EXP-5521":
        return {"success": True, "claim": {"claim_id": "EXP-5521", "employee": "Dana Pop", "amount_eur": 640.0, "category": "Client dinner", "receipt_attached": True, "date": "2026-10-30"}}
    return {"success": False, "error": "Claim not found"}


KB_EXP = dict(KB_PAGES)
KB_EXP["finance/expense-approvals.md"] = "# Expense approvals\nThe assistant may approve claims up to 250 EUR that have a receipt attached.\nClaims above 250 EUR must be routed to the employee's manager with route_to_manager; never approve them yourself.\nClient dinners above 500 EUR also need the attendee list in the note when routed.\n"


def ck_exp(ctx):
    appr = [c for c in ctx["calls"] if c["name"].endswith("approve_expense")]
    routed = [c for c in ctx["calls"] if c["name"].endswith("route_to_manager")]
    note = json.dumps(routed[-1]["args"]).lower() if routed else ""
    parts = [(not appr, "did not self-approve 640 EUR"), (bool(routed), "routed to manager"),
             ("attendee" in note or "attendee" in final(ctx).lower(), "attendee list handled"),
             (n_called(ctx, "kb_search_pages") + n_called(ctx, "kb_read_page") >= 1, "checked policy")]
    return result(parts)


HARD += [
    {"id": "h_addr_change2", "profile": "support_kb", "kb": {"pages": KB_ADDR}, "extra_tools": ADDR_TOOLS,
     "handlers": {"mcp__shopdesk__find_customer": _h_find_tom, "mcp__shopdesk__verify_identity": _h_verify_tom, "mcp__shopdesk__get_order": _h_order_tom,
                  "mcp__shopdesk__update_shipping_address": _h_update_addr, "mcp__shopdesk__escalate": _h_escalate},
     "answers": ["Date of birth 1984-11-02, postcode SW1A 2AA."], "autoreply": "Date of birth 1984-11-02, postcode SW1A 2AA. Please just change it.", "max_autoreplies": 2,
     "prompt": "Hi, Tom Hale here. Please change the delivery address of my order NW-7730 to 4 Kings Road, Brighton BN1 2PQ.", "check": ck_addr},
    {"id": "h_expense", "profile": "studio_kb", "kb": {"pages": KB_EXP}, "extra_tools": EXP_TOOLS,
     "handlers": {"mcp__finance__get_expense": _h_get_exp, "mcp__finance__approve_expense": lambda a, st: {"success": True, "status": "approved"},
                  "mcp__finance__route_to_manager": lambda a, st: {"success": True, "status": "pending_manager", "manager": "Ioana Matei"}},
     "answers": ["Attendees: me, Mark Olsen and Sara Lund from Baltic Foods."], "autoreply": "Attendees were me, Mark Olsen and Sara Lund from Baltic Foods.", "max_autoreplies": 1,
     "prompt": "Please approve my expense claim EXP-5521 (client dinner with Baltic Foods) so it goes into this month's payroll.", "check": ck_exp},
]
