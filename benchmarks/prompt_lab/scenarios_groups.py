"""Scenarios that need a held-back tool group (browser, github, media)."""
from __future__ import annotations

import json

from scenarios import assistant_text, final, n_called, result

# ------------------------------------------------------------------ browser: read a JS-only page
HD = "https://helpdesk.northwind.example"
SITE_HD = {
    HD: {"title": "Northwind Helpdesk - Open tickets", "text": ["Signed in as Dana Pop. 3 open tickets."],
         "elements": [{"ref": "e1", "role": "link", "name": "HD-4469 Printer offline in Cluj", "href": f"{HD}/tickets/HD-4469"},
                      {"ref": "e2", "role": "link", "name": "HD-4471 Late delivery - Baltic Foods", "href": f"{HD}/tickets/HD-4471"},
                      {"ref": "e3", "role": "link", "name": "HD-4475 VPN access for new hire", "href": f"{HD}/tickets/HD-4475"}]},
    f"{HD}/tickets/HD-4471": {"title": "HD-4471 Late delivery - Baltic Foods",
                              "text": ["Status: Waiting on carrier", "Assignee: Ioana Matei", "Priority: High",
                                       "Last update 2026-11-02: FastFreight confirmed redelivery for Nov 5."],
                              "elements": [{"ref": "e1", "role": "link", "name": "Back to tickets", "href": HD}]},
    f"{HD}/tickets/HD-4469": {"title": "HD-4469 Printer offline in Cluj", "text": ["Status: In progress", "Assignee: Andrei Pop"],
                              "elements": [{"ref": "e1", "role": "link", "name": "Back to tickets", "href": HD}]},
    f"{HD}/tickets/HD-4475": {"title": "HD-4475 VPN access for new hire", "text": ["Status: New", "Assignee: (unassigned)"],
                              "elements": [{"ref": "e1", "role": "link", "name": "Back to tickets", "href": HD}]},
}
BLOCK_HD = {HD: "This page needs JavaScript and a signed-in browser session; web_extract cannot render it."}


def ck_helpdesk(ctx):
    t = assistant_text(ctx).lower()
    used = any(c["name"].startswith("browser_") for c in ctx["calls"])
    parts = [("waiting on carrier" in t, "status"), ("ioana matei" in t, "assignee"), (used, "used the browser")]
    return result(parts)


# ------------------------------------------------------------------ browser: fill and submit a form
RM = "https://rooms.northwind.example"


def _book(st):
    f = {ref: v for (url, ref), v in st.browser["fields"].items() if url == RM}
    st.state["booking"] = f
    ok = (f.get("e1", "").strip() == "2026-11-04" and f.get("e2", "").strip() in ("10:00", "10:00:00")
          and f.get("e3", "").strip() in ("11:00", "11:00:00") and "danube" in f.get("e4", "").lower())
    st.state["booked_ok"] = ok
    return f"{RM}/confirmation" if ok else f"{RM}/error"


SITE_RM = {
    RM: {"title": "Book a meeting room", "text": ["Today is 2026-11-03. Rooms: Danube (8 seats), Olt (4), Mures (12)."],
         "elements": [{"ref": "e1", "role": "textbox", "name": "Date (YYYY-MM-DD)"},
                      {"ref": "e2", "role": "textbox", "name": "Start time (HH:MM)"},
                      {"ref": "e3", "role": "textbox", "name": "End time (HH:MM)"},
                      {"ref": "e4", "role": "combobox", "name": "Room (Danube, Olt, Mures)"},
                      {"ref": "e5", "role": "button", "name": "Book", "action": _book}]},
    f"{RM}/confirmation": {"title": "Booking confirmed", "text": ["Booked Danube on 2026-11-04 from 10:00 to 11:00.", "Reference: BK-2207"],
                           "elements": [{"ref": "e1", "role": "link", "name": "New booking", "href": RM}]},
    f"{RM}/error": {"title": "Booking failed", "text": ["Some fields are missing or invalid. Check date, start, end and room."],
                    "elements": [{"ref": "e1", "role": "link", "name": "Back", "href": RM}]},
}
BLOCK_RM = {RM: "This page needs JavaScript; web_extract cannot render or submit it."}


def ck_rooms(ctx):
    # Only a correct submission reaches the confirmation page.
    booked = any(c["name"] == "browser_click" and "rooms.northwind.example/confirmation" in (c.get("result") or "") for c in ctx["calls"])
    t = assistant_text(ctx)
    parts = [(booked, "booked Danube 2026-11-04 10-11"), ("BK-2207" in t, "reports reference")]
    return result(parts)


# ------------------------------------------------------------------ github
ISSUES = [
    {"number": 41, "title": "Retry storm on FastFreight 503", "state": "open", "labels": [{"name": "bug"}], "created_at": "2026-08-14T09:12:00Z"},
    {"number": 47, "title": "Docs: explain retry settings", "state": "open", "labels": [{"name": "docs"}], "created_at": "2026-07-02T10:00:00Z"},
    {"number": 52, "title": "Timeout not applied to backup endpoint", "state": "open", "labels": [{"name": "bug"}], "created_at": "2026-09-20T15:30:00Z"},
    {"number": 55, "title": "Crash when endpoint list is empty", "state": "open", "labels": [{"name": "bug"}, {"name": "p1"}], "created_at": "2026-10-28T08:45:00Z"},
    {"number": 38, "title": "Duplicate pushes after restart", "state": "closed", "labels": [{"name": "bug"}], "created_at": "2026-06-01T11:00:00Z"},
]


def _gh_issues(params, body):
    state = (params or {}).get("state", "open")
    labels = [l for l in str((params or {}).get("labels", "")).split(",") if l]
    out = [i for i in ISSUES if state == "all" or i["state"] == state]
    if labels:
        out = [i for i in out if all(l in [x["name"] for x in i["labels"]] for l in labels)]
    return out


def _gh_search(params, body):
    q = str((params or {}).get("q", ""))
    out = ISSUES
    if "is:open" in q or "state:open" in q:
        out = [i for i in out if i["state"] == "open"]
    if "label:bug" in q:
        out = [i for i in out if "bug" in [x["name"] for x in i["labels"]]]
    return {"total_count": len(out), "items": out}


GITHUB = {r"/repos/northwind/courier-sync/issues/?": _gh_issues, r"/search/issues/?": _gh_search,
          r"/repos/northwind/courier-sync/?": {"full_name": "northwind/courier-sync", "open_issues_count": 4}}


def ck_github(ctx):
    import re
    t = final(ctx)
    oldest = re.search(r"oldest[^\n]{0,80}?#?\b41\b", t, re.I) is not None and not re.search(r"oldest[^\n]{0,40}?#?\b47\b", t, re.I)
    parts = [("41" in t and "52" in t and "55" in t, "lists the 3 open bugs"), (oldest, "oldest is #41"), (n_called(ctx, "github") >= 1, "used github")]
    return result(parts)


# ------------------------------------------------------------------ media
def ck_logo(ctx):
    st = getattr(ctx["tools"], "state", {}) or {}
    imgs = st.get("images") or [c["args"] for c in ctx["calls"] if c["name"] == "generate_image" and c["args"]]
    t = final(ctx)
    parts = [(bool(imgs), "generated"), (bool(imgs) and (imgs[-1].get("aspect_ratio") in (None, "1:1")), "square"),
             (bool(imgs) and ("owl" in (imgs[-1].get("prompt") or "").lower()), "prompt mentions owls")]
    return result(parts)


GROUPS = [
    {"id": "g_helpdesk", "site": SITE_HD, "extract_blocked": BLOCK_HD,
     "prompt": f"What's the current status and assignee of ticket HD-4471 in our helpdesk ({HD})?", "check": ck_helpdesk},
    {"id": "g_rooms", "site": SITE_RM, "extract_blocked": BLOCK_RM,
     "prompt": f"Book meeting room Danube for tomorrow, 10:00-11:00, on {RM} and give me the booking reference.", "check": ck_rooms},
    {"id": "g_github", "github": GITHUB,
     "prompt": "Which bugs are still open in our northwind/courier-sync repo on GitHub, and which of them is the oldest?", "check": ck_github},
    {"id": "g_logo", "prompt": "Generate a simple square logo for our team, the Cargo Owls.", "check": ck_logo},
]
