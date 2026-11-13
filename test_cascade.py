"""End-to-end check of the fact store and cascade, without calling Claude.

    python test_cascade.py

Uses a throwaway database seeded from the spreadsheet and hand-written facts that
stand in for what extract.py returns.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
os.environ["EVENTS_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

import cascade                                    # noqa: E402
from init_db import get_conn, init_db             # noqa: E402

init_db()
subprocess.run([sys.executable, str(HERE / "seed.py")], check=True, stdout=subprocess.DEVNULL)
conn = get_conn()


def source(kind, sender, received_at):
    return conn.execute(
        "INSERT INTO sources(event_id, kind, sender, received_at, raw_text) VALUES (1,?,?,?,?)",
        (kind, sender, received_at, f"{kind} {received_at}")).lastrowid


def fact(key, value, unit="", quote="q", confidence=0.95):
    return {"key": key, "value": value, "unit": unit, "quote": quote, "confidence": confidence}


def flagged():
    return {r["title"].lower(): r["flag_reason"] for r in
            conn.execute("SELECT title, flag_reason FROM tasks WHERE status = 'needs_recheck'")}


eid = cascade.create_event(conn, "Test summit", "Corporate Event")
assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] > 0, "no tasks created from templates"

# 1. Contract and kickoff email: first-seen facts are confirmed, nothing is flagged
r = cascade.record_facts(conn, eid, source("contract", "Venue", "2026-11-02"), [
    fact("event_start_date", "2027-02-18"), fact("venue_capacity_seated", "120", "guests"),
    fact("fb_minimum", "9000", "USD"), fact("headcount_guarantee_deadline", "2027-02-04"),
    fact("not_a_real_key", "x"),
])
assert len(r["new"]) == 4 and r["ignored"] == ["not_a_real_key"], r
assert "arrange a coat check" in flagged(), "winter date should add the winter tasks"
assert "Learned from" in flagged()["arrange a coat check"]
r = cascade.record_facts(conn, eid, source("email", "Dana", "2026-11-03"), [
    fact("headcount_expected", "100", "guests"), fact("event_start_date", "2027-02-18"),
    fact("budget_total", "25,000", "USD", confidence=0.4),
])
assert r["new"] == ["headcount_expected"] and r["unchanged"] == ["event_start_date"], r
assert r["proposed"] == ["budget_total"], "low-confidence first-seen fact should wait for approval"
assert len(flagged()) == 4, flagged()                # only the winter tasks so far
due = conn.execute("SELECT due_date FROM tasks WHERE title = 'Secure Food and Bev'").fetchone()[0]
assert due == "2026-10-06", due                   # 135 days before Feb 18

# 2. Transcript says 150: proposed only, nothing changes until approved
r = cascade.record_facts(conn, eid, source("transcript", "Planning call", "2026-11-12"), [
    fact("headcount_expected", "150", "guests", quote="We're now planning for 150 guests, not 100."),
])
assert r["proposed"] == ["headcount_expected"] and len(flagged()) == 4
assert cascade.current_facts(conn, eid)["headcount_expected"]["value"] == "100"

# 3. Approve: dependent tasks flagged with the source, capacity and deadline alerts added
pid = conn.execute("SELECT id FROM facts WHERE key='headcount_expected' AND status='proposed'").fetchone()[0]
r = cascade.approve(conn, pid)
f = flagged()
assert "secure food and bev" in f and "book venue" in f, f.keys()
assert "from 100 guests to 150 guests" in f["secure food and bev"]
assert "now planning for 150" in f["secure food and bev"]
assert "fb minimum = $9,000" in f["secure food and bev"], f["secure food and bev"]
assert "vendor contacts" not in f, "task that doesn't depend on headcount was flagged"
assert "Headcount 150 Is Over the Venue's Seated Capacity of 120" in r["added"], r["added"]
assert any("Send the Venue the New Headcount (150) by 02-04-2027" in t for t in r["added"]), r["added"]
assert "Contact Vendor About Increase or Decrease in Headcount" in r["added"], r["added"]
statuses = [x[0] for x in conn.execute("SELECT status FROM facts WHERE key='headcount_expected' ORDER BY id")]
assert statuses == ["superseded", "confirmed"], statuses

# 4. Date moves to May: due dates shift and the season consideration fires
cascade.record_facts(conn, eid, source("email", "Venue", "2026-11-13"), [fact("event_start_date", "2027-05-13")])
pid = conn.execute("SELECT id FROM facts WHERE key='event_start_date' AND status='proposed'").fetchone()[0]
r = cascade.approve(conn, pid)
due = conn.execute("SELECT due_date FROM tasks WHERE title = 'Secure Food and Bev'").fetchone()[0]
assert due == "2026-12-29", due
assert "Plan for Rain: Covered Arrival and a Backup for Any Outdoor Space" in r["added"], r["added"]
assert "Arrange a Coat Check" in r["flagged"], "winter task should be flagged when the date leaves winter"
assert "may no longer be needed" in flagged()["arrange a coat check"]

# 4b. Dates: any written format compares equal and is shown as MM-DD-YYYY
assert cascade.same_value("2027-04-18", "4/18/27") and not cascade.same_value("2027-04-18", "4/19/27")
assert cascade.fmt("2027-04-18") == "04-18-2027"

# 4c. Task names are capitalised, acronyms kept
assert cascade.title_case("AV needs send to venue") == "AV Needs Send to Venue"
assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'Hire Photographer for Event'").fetchone()[0] == 1

# 4d. Typing a new value into a fact stores it, with the planner as the source, and cascades
r = cascade.set_fact(conn, eid, "venue_capacity_seated", "160")
cur = cascade.current_facts(conn, eid)["venue_capacity_seated"]
assert cur["value"] == "160" and cur["unit"] == "guests", dict(cur)
assert "Book Venue" in r["flagged"], r
assert "manual edit from the event planner" in flagged()["book venue"]
r = cascade.set_fact(conn, eid, "event_start_date", "05-14-2027")
assert cascade.current_facts(conn, eid)["event_start_date"]["value"] == "2027-05-14"
try:
    cascade.set_fact(conn, eid, "event_start_date", "next spring"); raise SystemExit("bad date accepted")
except ValueError:
    pass

# 4e. End date is optional: same-day end dates are not stored, and any fact can be blanked
r = cascade.record_facts(conn, eid, source("email", "Venue", "2026-11-15"), [
    fact("event_start_date", "2027-05-14"), fact("event_end_date", "2027-05-14")])
assert "event_end_date" not in cascade.current_facts(conn, eid) and not r["proposed"], r
cascade.set_fact(conn, eid, "event_end_date", "05-15-2027")
assert cascade.current_facts(conn, eid)["event_end_date"]["value"] == "2027-05-15"
cascade.set_fact(conn, eid, "event_end_date", "")
assert "event_end_date" not in cascade.current_facts(conn, eid)

# 5. Reject leaves the current value alone
cascade.record_facts(conn, eid, source("email", "Someone", "2026-11-14"), [fact("fb_minimum", "12000", "USD")])
pid = conn.execute("SELECT id FROM facts WHERE key='fb_minimum' AND status='proposed'").fetchone()[0]
cascade.reject(conn, pid)
assert cascade.current_facts(conn, eid)["fb_minimum"]["value"] == "9000"

print(f"All cascade checks passed ({len(flagged())} tasks flagged at the end)")
