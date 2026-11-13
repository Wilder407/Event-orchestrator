"""Light review screen for the Event Cascade Agent.

    python app.py            # then open http://localhost:8000
    python app.py 8080       # different port

Standard library only (no web framework). The page lives in static/index.html.
"""
import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cascade
from init_db import get_conn, init_db

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
DEMO_EVENT = ("Northwind Annual Client Summit", "Corporate Event")


def parse_sample(path):
    """Sample files are 'key: value' header lines, a --- line, then the document."""
    head, _, body = path.read_text().partition("\n---\n")
    meta = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
    return {"file": path.name, "title": meta.get("title", path.name), "kind": meta.get("kind", "email"),
            "sender": meta.get("sender"), "received_at": meta.get("received_at"), "text": body.strip(),
            "baseline": meta.get("baseline", "").lower() == "yes"}


def get_event(conn):
    """The MVP tracks one event; create the demo event on first use."""
    row = conn.execute("SELECT * FROM events ORDER BY id LIMIT 1").fetchone()
    if row:
        return row
    if not conn.execute("SELECT 1 FROM task_templates LIMIT 1").fetchone():
        raise RuntimeError("No task templates in the database. Run: python seed.py")
    event_id = cascade.create_event(conn, *DEMO_EVENT)
    load_baseline(conn, event_id)
    return conn.execute("SELECT * FROM events ORDER BY id LIMIT 1").fetchone()


NOTICE = {"text": None}


def load_baseline(conn, event_id):
    """Read the 'baseline: yes' samples so the event facts are filled in before any new source arrives."""
    NOTICE["text"] = None
    for s in (parse_sample(p) for p in sorted(SAMPLES.glob("*.txt"))):
        if not s["baseline"]:
            continue
        try:
            _ingest(conn, event_id, s["kind"], s["sender"], s["received_at"], s["text"])
        except Exception as e:                    # page still loads; the document stays in the inbox
            NOTICE["text"] = f"Could not load starting information from {s['title']}: {e}"


def state(conn):
    event = get_event(conn)
    eid = event["id"]
    facts = conn.execute(
        """SELECT f.*, s.kind AS source_kind, s.sender AS source_sender FROM facts f
           LEFT JOIN sources s ON s.id = f.source_id
           WHERE f.event_id = ? AND f.status IN ('confirmed','proposed') ORDER BY f.key""", (eid,)).fetchall()
    current = {f["key"]: f for f in facts if f["status"] == "confirmed"}
    proposals = []
    for f in facts:
        if f["status"] == "proposed":
            p = dict(f)
            old = current.get(f["key"])
            p["old_display"] = cascade.fmt(old["value"], old["unit"]) if old else None
            p["new_backing"] = 1 + conn.execute(
                "SELECT COUNT(DISTINCT source_id) FROM fact_mentions WHERE fact_id = ?", (f["id"],)).fetchone()[0]
            # how many sources back the current value: the original plus later ones that agreed
            p["old_backing"] = 1 + conn.execute(
                "SELECT COUNT(DISTINCT source_id) FROM fact_mentions WHERE fact_id = ?", (old["id"],)).fetchone()[0] if old else 0
            p["affects"] = conn.execute(
                """SELECT COUNT(*) FROM tasks t JOIN task_templates tt ON tt.id = t.template_id, json_each(tt.depends_on) d
                   WHERE t.event_id = ? AND t.status IN ('ok','needs_recheck') AND d.value = ?""",
                (eid, f["key"])).fetchone()[0]
            proposals.append(p)
    for group in (proposals, facts):
        for i, f in enumerate(group):
            f = dict(f)
            f["label"] = cascade.label(f["key"])
            f["display"] = cascade.fmt(f["value"], f["unit"])
            group[i] = f
    by_key = {f["key"]: f for f in facts if f["status"] == "confirmed"}
    pending_keys = {p["key"] for p in proposals}
    board = []
    for (key,) in conn.execute("SELECT key FROM fact_keys ORDER BY rowid").fetchall():
        prev = conn.execute(
            "SELECT value, unit FROM facts WHERE event_id = ? AND key = ? AND status = 'superseded' ORDER BY id DESC LIMIT 1",
            (eid, key)).fetchone()
        f = by_key.get(key)
        board.append({
            "key": key, "label": cascade.label(key), "display": f["display"] if f else None,
            # what the edit box starts with: the date as shown, otherwise the bare value
            "edit": (f["display"] if cascade._date(f["value"]) else f["value"]) if f else "",
            "was": cascade.fmt(prev["value"], prev["unit"]) if f and prev else None,
            "pending": key in pending_keys, "quote": f["quote"] if f else None,
            "source": " from ".join(x for x in (f["source_kind"], f["source_sender"]) if x) if f else None,
        })
    tasks = conn.execute("SELECT * FROM tasks WHERE event_id = ? ORDER BY due_date IS NULL, due_date, id", (eid,)).fetchall()
    sources = conn.execute("SELECT id, kind, sender, received_at, raw_text FROM sources WHERE event_id = ? ORDER BY id", (eid,)).fetchall()
    ingested = {s["raw_text"] for s in sources}
    samples = [parse_sample(p) for p in sorted(SAMPLES.glob("*.txt"))]
    return {
        "event": dict(event),
        "today": datetime.now().date().isoformat(),
        "proposals": proposals,
        "notice": NOTICE["text"],
        "board": board,
        "facts": [f for f in facts if f["status"] == "confirmed"],
        "tasks": [{**dict(t), "due_display": cascade.fmt(t["due_date"]) if t["due_date"] else ""} for t in tasks],
        "sources": [{k: s[k] for k in ("id", "kind", "sender", "received_at")} for s in sources],
        "samples": [{**{k: s[k] for k in ("file", "title", "kind", "baseline")}, "ingested": s["text"] in ingested} for s in samples],
    }


def ingest(conn, kind, sender, received_at, text):
    return _ingest(conn, get_event(conn)["id"], kind, sender, received_at, text)


def _ingest(conn, event_id, kind, sender, received_at, text):
    from extract import extract_facts
    if conn.execute("SELECT 1 FROM sources WHERE event_id = ? AND raw_text = ?", (event_id, text)).fetchone():
        raise ValueError("That source has already been read")
    keys = [r["key"] for r in conn.execute("SELECT key FROM fact_keys")]
    received_at = received_at or datetime.now().isoformat(timespec="seconds")
    known = {k: f["value"] for k, f in cascade.current_facts(conn, event_id).items()}
    extracted = extract_facts(text, kind, received_at, keys, known)
    source_id = conn.execute(
        "INSERT INTO sources(event_id, kind, sender, received_at, raw_text) VALUES (?,?,?,?,?)",
        (event_id, kind, sender, received_at, text)).lastrowid
    return cascade.record_facts(conn, event_id, source_id, extracted)


def reset(conn):
    for table in ("tasks", "fact_mentions", "facts", "sources", "events"):
        conn.execute(f"DELETE FROM {table}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self._send((HERE / "static" / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._run(lambda conn, body: state(conn))
        self._send({"error": "Not found"}, 404)

    def do_POST(self):
        parts = self.path.strip("/").split("/")          # api/facts/12/approve
        routes = {
            ("ingest",): lambda c, b: ingest(c, **(parse_sample_fields(b))),
            ("reset",): lambda c, b: reset(c),
            ("facts", "set"): lambda c, b: cascade.set_fact(c, get_event(c)["id"], b.get("key"), b.get("value", "")),
            ("facts", "approve"): lambda c, b: decide_many(c, b.get("ids") or [], cascade.approve),
            ("facts", "reject"): lambda c, b: decide_many(c, b.get("ids") or [], cascade.reject),
        }
        if parts[:1] != ["api"]:
            return self._send({"error": "Not found"}, 404)
        if tuple(parts[1:]) in routes:
            return self._run(routes[tuple(parts[1:])])
        if len(parts) == 4 and parts[1] == "facts" and parts[3] in ("approve", "reject"):
            fn = cascade.approve if parts[3] == "approve" else cascade.reject
            return self._run(lambda c, b: fn(c, int(parts[2])))
        if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "status":
            return self._run(lambda c, b: set_task_status(c, int(parts[2]), b.get("status")))
        self._send({"error": "Not found"}, 404)

    def _run(self, fn):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            conn = get_conn()
            try:
                with conn:                                # commit on success, roll back on error
                    result = fn(conn, body)
                with conn:
                    out = state(conn) if self.command == "POST" else result
                    if self.command == "POST":
                        out["result"] = result
            finally:
                conn.close()
            self._send(out)
        except Exception as e:                            # show the reason on the page
            self._send({"error": f"{type(e).__name__}: {e}"}, 400)

    def log_message(self, *args):
        pass


def parse_sample_fields(body):
    if body.get("sample"):
        path = SAMPLES / Path(body["sample"]).name
        s = parse_sample(path)
        return {k: s[k] for k in ("kind", "sender", "received_at", "text")}
    if not (body.get("text") or "").strip():
        raise ValueError("Paste some text first")
    return {"kind": body.get("kind") or "email", "sender": body.get("sender") or None,
            "received_at": None, "text": body["text"].strip()}


def decide_many(conn, ids, fn):
    """Approve or ignore several proposed facts in one go."""
    if not ids:
        raise ValueError("Tick at least one change first")
    total = {"flagged": set(), "added": []}
    for fact_id in ids:
        r = fn(conn, int(fact_id)) or {}
        total["flagged"].update(r.get("flagged", []))
        total["added"] += r.get("added", [])
    return {"flagged": sorted(total["flagged"]), "added": total["added"]}


def set_task_status(conn, task_id, status):
    if status not in ("ok", "done", "dismissed"):
        raise ValueError(f"Unknown status {status}")
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    init_db()
    print(f"Review screen: http://localhost:{port}   (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
