"""Load fact keys, task templates and considerations from Event_orchestrator.xlsx.

Usage:
    pip install openpyxl
    python init_db.py --reset
    python seed.py                       # reads Event_orchestrator.xlsx
    python seed.py path/to/other.xlsx

Edit the spreadsheet, then re-run both commands to reseed.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

from init_db import get_conn

HERE = Path(__file__).parent
XLSX = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "Event_orchestrator.xlsx"

NOT_FACTS = {"task_id"}          # rows on fact_sheet that aren't event facts
MAX_OFFSET_DAYS = 400            # anything further out is probably a typo'd date

def clean(v):
    return v.strip() if isinstance(v, str) else v


def load_fact_keys(wb):
    keys = []
    for (v,) in wb["fact_sheet"].iter_rows(min_col=1, max_col=1, values_only=True):
        v = clean(v)
        if v and v not in NOT_FACTS:
            keys.append(v)
    return keys


def load_templates(ws, valid_keys, warnings):
    event_type = clean(ws["C1"].value)
    event_date = ws["G2"].value
    header = [clean(c.value) for c in ws[3]]
    col = {name: i for i, name in enumerate(header) if name}
    rows = []
    for r in ws.iter_rows(min_row=4, values_only=True):
        code = clean(r[col["TaskID"]])
        if not code:
            continue
        due = r[col["Due Date"]]
        offset = (due - event_date).days if isinstance(due, datetime) else None
        if offset is not None and abs(offset) > MAX_OFFSET_DAYS:
            warnings.append(f"{code}: due date {due:%Y-%m-%d} is {abs(offset)} days out; offset left blank")
            offset = None
        deps_raw = clean(r[col["Depends On (fact keys)"]]) if "Depends On (fact keys)" in col else ""
        deps = [d.strip() for d in (deps_raw or "").split(",") if d.strip()]
        for d in deps:
            if d not in valid_keys:
                warnings.append(f"{code}: unknown fact key '{d}'")
        rows.append((
            code, event_type, clean(r[col["Stage"]]), clean(r[col["Who"]]),
            clean(r[col["Task"]]), clean(r[col["Notes"]]), json.dumps(deps), offset,
        ))
    return rows


SEASONS = {"winter", "spring", "summer", "fall"}


def load_considerations(ws, valid_keys, warnings):
    """Columns: Consideration | Action | When (season) | Tasks (one per line) |
    Learned from | Fires when these facts change.

    A row with a season in 'When' adds its tasks whenever the event date lands in
    that season. Other rows add their tasks when any listed fact key changes.
    """
    header = [clean(c.value) for c in ws[1]]
    col = {name: i for i, name in enumerate(header) if name}

    def cell(r, name):
        return clean(r[col[name]]) if name in col and col[name] < len(r) else None

    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        title, action = cell(r, "Consideration"), cell(r, "Action")
        if not title:
            continue
        season = (cell(r, "When (season)") or "").lower()
        tasks = [t.strip() for t in (cell(r, "Tasks (one per line)") or "").splitlines() if t.strip()]
        if season:
            if season not in SEASONS:
                warnings.append(f"Consideration '{title}': unknown season '{season}'")
                continue
            trig = {"on_change": ["event_start_date"], "season": season}
        else:
            keys = [k.strip() for k in (cell(r, "Fires when these facts change") or "").split(",") if k.strip()]
            for k in keys:
                if k not in valid_keys:
                    warnings.append(f"Consideration '{title}': unknown fact key '{k}'")
            if not keys:
                warnings.append(f"Consideration '{title}' will never fire: fill in "
                                f"'When (season)' or 'Fires when these facts change'")
            trig = {"on_change": keys}
        rows.append((title, json.dumps(trig), action, cell(r, "Learned from"), json.dumps(tasks)))
    return rows


def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    warnings = []

    keys = load_fact_keys(wb)
    templates = []
    for ws in wb.worksheets:
        if ws.title.startswith("task_template"):
            templates += load_templates(ws, set(keys), warnings)
    considerations = load_considerations(wb["considerations"], set(keys), warnings)

    with get_conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO fact_keys(key) VALUES (?)", [(k,) for k in keys])
        conn.executemany(
            """INSERT OR REPLACE INTO task_templates
               (code, event_type, stage, owner_role, title, notes, depends_on, due_offset_days)
               VALUES (?,?,?,?,?,?,?,?)""",
            templates,
        )
        conn.execute("DELETE FROM considerations")
        conn.executemany(
            "INSERT INTO considerations(title, triggers, action, origin_event, tasks) VALUES (?,?,?,?,?)",
            considerations,
        )

    print(f"Seeded {len(keys)} fact keys, {len(templates)} task templates, "
          f"{len(considerations)} considerations")
    for w in warnings:
        print("WARNING:", w)


if __name__ == "__main__":
    main()
