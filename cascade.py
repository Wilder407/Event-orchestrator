"""Fact store, change detection and cascade. No AI in here: plain rules over SQLite.

Flow:
    record_facts()  new source's facts -> first-seen values are confirmed,
                    changed values wait as 'proposed' for a person to approve
    approve()       proposed -> confirmed, old value -> superseded, then cascade()
    cascade()       flags every task that depends on the changed fact, adds
                    situational tasks (considerations) and checks hard constraints
"""
import json
from datetime import date, datetime, timedelta

AUTO_CONFIRM_MIN_CONFIDENCE = 0.7   # first-seen facts below this wait for approval
OPEN = ("ok", "needs_recheck")


# ---------- small helpers ----------

def label(key):
    return key.replace("_", " ")


SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "vs", "with"}


def title_case(text):
    """'Hire photographer for event' -> 'Hire Photographer for Event'. Acronyms (AV, F&B) are kept."""
    out, start = [], True
    for word in text.strip().split():
        if word.lower() in SMALL_WORDS and not start:
            out.append(word.lower())
        else:
            # capitalise the first letter of each hyphenated part, leave the rest as written
            out.append("-".join(part[:1].upper() + part[1:] for part in word.split("-")))
        start = word.endswith((":", ".", "?", "!"))
    return " ".join(out)


def show_date(d):
    """Dates are stored as YYYY-MM-DD and shown as MM-DD-YYYY."""
    return f"{d:%m-%d-%Y}"


def fmt(value, unit=None):
    if _date(value):
        return show_date(_date(value))
    if unit and unit.upper() == "USD":
        try:
            return f"${float(value):,.0f}"
        except ValueError:
            return f"${value}"
    return f"{value} {unit}" if unit else str(value)


def _norm(v):
    if _date(v):
        return _date(v).isoformat()
    s = str(v).strip().lower().replace(",", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return " ".join(s.split())


def same_value(a, b):
    return _norm(a) == _norm(b)


def _num(row):
    try:
        return float(_norm(row["value"])) if row else None
    except (TypeError, ValueError):
        return None


def _date(value):
    """Read 2027-04-19, 4/19/27, 04-19-2027 or 4/19/2027 as a date; anything else is None."""
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _season(d):
    return ["winter", "winter", "spring", "spring", "spring", "summer",
            "summer", "summer", "fall", "fall", "fall", "winter"][d.month - 1]


def current_facts(conn, event_id):
    rows = conn.execute("SELECT * FROM current_facts WHERE event_id = ?", (event_id,))
    return {r["key"]: r for r in rows}


def _cite(conn, fact):
    """'email from Dana Whitfield: "quote"' for a fact row."""
    src = conn.execute("SELECT kind, sender FROM sources WHERE id = ?", (fact["source_id"],)).fetchone()
    where = f"{src['kind']} from {src['sender']}" if src and src["sender"] else (src["kind"] if src else "unknown source")
    return f'{where}: "{fact["quote"]}"' if fact["quote"] else where


# ---------- events and tasks ----------

def create_event(conn, name, event_type):
    """Create the event and its checklist from the org's standard task templates."""
    event_id = conn.execute("INSERT INTO events(name, type) VALUES (?, ?)", (name, event_type)).lastrowid
    add_template_tasks(conn, event_id, event_type)
    return event_id


def add_template_tasks(conn, event_id, event_type):
    for t in conn.execute("SELECT * FROM task_templates WHERE event_type = ?", (event_type,)).fetchall():
        conn.execute("INSERT INTO tasks(event_id, template_id, title, stage, owner_role) VALUES (?,?,?,?,?)",
                     (event_id, t["id"], title_case(t["title"]), t["stage"], t["owner_role"]))


def _set_due_dates(conn, event_id, start):
    rows = conn.execute(
        """SELECT t.id, tt.due_offset_days AS off FROM tasks t
           JOIN task_templates tt ON tt.id = t.template_id
           WHERE t.event_id = ? AND tt.due_offset_days IS NOT NULL""", (event_id,)).fetchall()
    for r in rows:
        conn.execute("UPDATE tasks SET due_date = ? WHERE id = ?",
                     ((start + timedelta(days=r["off"])).isoformat(), r["id"]))


def _flag(conn, task, reason, fact_id):
    reason = f"{task['flag_reason']}\n{reason}" if task["status"] == "needs_recheck" and task["flag_reason"] else reason
    conn.execute("UPDATE tasks SET status = 'needs_recheck', flag_reason = ?, flag_fact_id = ? WHERE id = ?",
                 (reason, fact_id, task["id"]))


def _add_alert(conn, event_id, title, reason, fact_id, consideration_id=None):
    """Add a flagged task unless an open one with the same title already exists.

    Returns the task title as stored, or None if nothing was added.
    """
    title = title_case(title)
    dup = conn.execute(
        f"SELECT 1 FROM tasks WHERE event_id = ? AND title = ? AND status IN {OPEN}", (event_id, title)).fetchone()
    if dup:
        return None
    conn.execute(
        """INSERT INTO tasks(event_id, consideration_id, title, stage, owner_role, status, flag_reason, flag_fact_id)
           VALUES (?, ?, ?, 'Needs attention', 'Event Planner', 'needs_recheck', ?, ?)""",
        (event_id, consideration_id, title, reason, fact_id))
    return title


# ---------- fact store ----------

def record_facts(conn, event_id, source_id, extracted):
    """Compare a source's extracted facts with the store. Returns a summary dict."""
    valid = {r["key"] for r in conn.execute("SELECT key FROM fact_keys")}
    src = conn.execute("SELECT received_at FROM sources WHERE id = ?", (source_id,)).fetchone()
    out = {"new": [], "proposed": [], "unchanged": [], "ignored": []}

    batch_start = next((f.get("value") for f in extracted if f.get("key") == "event_start_date"), None)
    for f in extracted:
        key, value = f.get("key"), str(f.get("value", "")).strip()
        if key not in valid or not value:
            out["ignored"].append(key)
            continue
        if key == "event_end_date":
            # the end date stays blank unless the event really runs past its start date
            start = batch_start or (current_facts(conn, event_id).get("event_start_date") or {"value": None})["value"]
            if start and same_value(start, value):
                out["unchanged"].append(key)
                continue
        if _date(value):
            value = _date(value).isoformat()           # one stored format, whatever the source wrote
        cur = current_facts(conn, event_id).get(key)
        if cur and same_value(cur["value"], value):
            # another source agreeing with the current value: remember it as backing
            conn.execute("INSERT INTO fact_mentions(fact_id, source_id, quote) VALUES (?,?,?)",
                         (cur["id"], source_id, f.get("quote")))
            out["unchanged"].append(key)
            continue
        pending = conn.execute(
            "SELECT id, value FROM facts WHERE event_id = ? AND key = ? AND status = 'proposed'", (event_id, key)).fetchall()
        match = next((p for p in pending if same_value(p["value"], value)), None)
        if match:
            # a second source making the same proposal: count it as backing for the change
            conn.execute("INSERT INTO fact_mentions(fact_id, source_id, quote) VALUES (?,?,?)",
                         (match["id"], source_id, f.get("quote")))
            out["unchanged"].append(key)
            continue

        confidence = f.get("confidence")
        first_seen = cur is None and not pending
        auto = first_seen and (confidence is None or confidence >= AUTO_CONFIRM_MIN_CONFIDENCE)
        fact_id = conn.execute(
            """INSERT INTO facts(event_id, key, value, unit, status, source_id, quote, confidence, observed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (event_id, key, value, f.get("unit") or None, "confirmed" if auto else "proposed",
             source_id, f.get("quote"), confidence, src["received_at"])).lastrowid
        if auto:
            out["new"].append(key)
            new = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            cascade(conn, event_id, new, old=None)
        else:
            out["proposed"].append(key)
    return out


def approve(conn, fact_id):
    """Accept a proposed fact, retire the old value and cascade the change."""
    new = conn.execute("SELECT * FROM facts WHERE id = ? AND status = 'proposed'", (fact_id,)).fetchone()
    if new is None:
        raise ValueError(f"Fact {fact_id} is not waiting for approval")
    old = current_facts(conn, new["event_id"]).get(new["key"])
    if old:
        conn.execute("UPDATE facts SET status = 'superseded' WHERE id = ?", (old["id"],))
    conn.execute("UPDATE facts SET status = 'confirmed' WHERE id = ?", (fact_id,))
    # any other pending proposal for the same key is now stale
    conn.execute("UPDATE facts SET status = 'rejected' WHERE event_id = ? AND key = ? AND status = 'proposed'",
                 (new["event_id"], new["key"]))
    return cascade(conn, new["event_id"], new, old)


def set_fact(conn, event_id, key, value):
    """A person typed a new value for a fact: store it, with them as the source, and cascade."""
    value = str(value or "").strip()
    if not conn.execute("SELECT 1 FROM fact_keys WHERE key = ?", (key,)).fetchone():
        raise ValueError(f"Unknown fact '{key}'")
    old = current_facts(conn, event_id).get(key)
    if not value:
        # blank = no value on file; the old value is kept as history
        if old:
            conn.execute("UPDATE facts SET status = 'superseded' WHERE id = ?", (old["id"],))
        return {"flagged": [], "added": [], "cleared": bool(old)}
    if _date(value):
        value = _date(value).isoformat()
    elif old and _date(old["value"]):
        raise ValueError(f"'{value}' is not a date. Use MM-DD-YYYY, for example 04-18-2027")
    if old and same_value(old["value"], value):
        return {"flagged": [], "added": []}
    now = datetime.now().isoformat(timespec="seconds")
    source_id = conn.execute(
        "INSERT INTO sources(event_id, kind, sender, received_at, raw_text) VALUES (?, 'manual edit', 'the event planner', ?, ?)",
        (event_id, now, f"{key} set to {value} at {now}")).lastrowid
    fact_id = conn.execute(
        """INSERT INTO facts(event_id, key, value, unit, status, source_id, confidence, observed_at)
           VALUES (?,?,?,?,'proposed',?,1.0,?)""",
        (event_id, key, value, old["unit"] if old else None, source_id, now)).lastrowid
    return approve(conn, fact_id)


def reject(conn, fact_id):
    n = conn.execute("UPDATE facts SET status = 'rejected' WHERE id = ? AND status = 'proposed'", (fact_id,)).rowcount
    if not n:
        raise ValueError(f"Fact {fact_id} is not waiting for approval")


# ---------- cascade ----------

def cascade(conn, event_id, new, old):
    """Push one confirmed fact through everything that depends on it.

    Returns {"flagged": [task titles], "added": [new task titles]}.
    """
    key = new["key"]
    facts = current_facts(conn, event_id)
    result = {"flagged": [], "added": []}

    if key == "event_start_date" and _date(new["value"]):
        _set_due_dates(conn, event_id, _date(new["value"]))

    if old is not None:
        change = (f"{label(key).capitalize()} changed from {fmt(old['value'], old['unit'])} "
                  f"to {fmt(new['value'], new['unit'])} ({_cite(conn, new)})")

        # 1. Standard tasks that depend on this fact
        tasks = conn.execute(
            f"""SELECT t.*, tt.depends_on FROM tasks t JOIN task_templates tt ON tt.id = t.template_id
                WHERE t.event_id = ? AND t.status IN {OPEN}""", (event_id,)).fetchall()
        for t in tasks:
            deps = json.loads(t["depends_on"])
            if key not in deps:
                continue
            related = [f"{label(d)} = {fmt(facts[d]['value'], facts[d]['unit'])}"
                       for d in deps if d != key and d in facts]
            reason = change + (f". Check against: {'; '.join(related)}" if related else "")
            _flag(conn, t, reason, new["id"])
            result["flagged"].append(t["title"])

    # 2. Situational tasks (considerations) triggered by this fact
    for c in conn.execute("SELECT * FROM considerations").fetchall():
        trig = json.loads(c["triggers"] or "{}")
        if key not in trig.get("on_change", []):
            continue
        if "season" in trig:
            _season_consideration(conn, event_id, c, trig, facts, new, old, result)
        elif old is not None:
            learned = f". Learned from: {c['origin_event']}" if c["origin_event"] else ""
            reason = change + learned + (_related(conn, event_id, trig, facts, key) or ".")
            for title in json.loads(c["tasks"] or "[]") or [f"{c['title']}: {c['action'] or 'review'}"]:
                added = _add_alert(conn, event_id, title, reason, new["id"], c["id"])
                if added:
                    result["added"].append(added)

    # A "send the venue the new headcount by <date>" reminder follows the deadline when it moves
    if key == "headcount_guarantee_deadline" and old is not None and _date(new["value"]) and _date(old["value"]):
        was, now = show_date(_date(old["value"])), show_date(_date(new["value"]))
        for t in conn.execute(
                f"SELECT * FROM tasks WHERE event_id = ? AND template_id IS NULL AND title LIKE ? AND status IN {OPEN}",
                (event_id, f"% by {was}")).fetchall():
            conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (t["title"].replace(f"by {was}", f"by {now}"), t["id"]))
            _flag(conn, t, change, new["id"])
            result["flagged"].append(t["title"].replace(f"by {was}", f"by {now}"))

    # 3. Hard constraints between facts (checked on every confirmed fact)
    for title, reason in _constraint_problems(facts, key, changed=old is not None):
        added = _add_alert(conn, event_id, title, f"{reason} ({_cite(conn, new)})", new["id"])
        if added:
            result["added"].append(added)
    return result


def _related(conn, event_id, trig, facts, changed_key):
    """'. Check against: <related facts>. Tasks that may be affected: <titles>' for a consideration.

    The related facts come from the spreadsheet's 'Related facts' column; the tasks are
    the open checklist tasks that depend on any of them. Empty when none are listed.
    """
    keys = [k for k in trig.get("related", []) if k != changed_key]
    if not keys:
        return ""
    values = [f"{label(k)} = {fmt(facts[k]['value'], facts[k]['unit']) if k in facts else 'not on file yet'}"
              for k in keys]
    rows = conn.execute(
        f"""SELECT t.title, tt.depends_on FROM tasks t JOIN task_templates tt ON tt.id = t.template_id
            WHERE t.event_id = ? AND t.status IN {OPEN} ORDER BY t.id""", (event_id,)).fetchall()
    titles = [r["title"] for r in rows if set(keys) & set(json.loads(r["depends_on"]))]
    text = f". Check against: {'; '.join(values)}"
    return text + (f". Tasks that may be affected: {'; '.join(titles)}" if titles else "")


def _season_consideration(conn, event_id, c, trig, facts, new, old, result):
    """Add a season's lessons when the date lands in it; flag them when it moves out."""
    season = trig["season"]
    now, before = _date(new["value"]), _date(old["value"]) if old else None
    if not now:
        return
    is_in, was_in = _season(now) == season, bool(before) and _season(before) == season
    learned = f" Learned from: {c['origin_event']}." if c["origin_event"] else ""
    if is_in and not was_in:
        reason = (f"Added because the event date ({show_date(now)}) is in {season}.{learned} ({_cite(conn, new)})"
                  + _related(conn, event_id, trig, facts, new["key"]))
        for title in json.loads(c["tasks"] or "[]"):
            added = _add_alert(conn, event_id, title, reason, new["id"], c["id"])
            if added:
                result["added"].append(added)
    elif was_in and not is_in:
        reason = (f"The event moved from {season} to {_season(now)} ({show_date(now)}), "
                  f"so this may no longer be needed ({_cite(conn, new)})")
        for t in conn.execute(
                f"SELECT * FROM tasks WHERE event_id = ? AND consideration_id = ? AND status IN {OPEN}",
                (event_id, c["id"])).fetchall():
            _flag(conn, t, reason, new["id"])
            result["flagged"].append(t["title"])


def _constraint_problems(facts, changed_key, changed):
    """Yield (title, reason) for each rule the current facts break."""
    head, cap = _num(facts.get("headcount_expected")), _num(facts.get("venue_capacity_seated"))
    if changed_key in ("headcount_expected", "venue_capacity_seated") and head and cap and head > cap:
        yield (f"Headcount {head:.0f} is over the venue's seated capacity of {cap:.0f}",
               f"{head - cap:.0f} guests will not fit. Cap the guest list, ask the venue about a "
               f"standing layout, or change venue")

    deadline = facts.get("headcount_guarantee_deadline")
    if changed and changed_key == "headcount_expected" and head and deadline and _date(deadline["value"]):
        d = _date(deadline["value"])
        days = (d - date.today()).days
        when = f"{days} days left" if days >= 0 else f"deadline passed {-days} days ago"
        yield (f"Send the venue the new headcount ({head:.0f}) by {show_date(d)}",
               f"The venue needs the guaranteed headcount by {show_date(d)} ({when})")
