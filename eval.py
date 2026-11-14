"""Measure how well extraction works on the demo samples.

    python eval.py

Runs every sample in samples/ through the same pipeline as the app, in date order,
on a throwaway database (your events.db is not touched). After each sample every
proposed change is approved, as a planner would, so later samples see the same
"values on file" they would in the demo. Each sample's facts are then checked
against expected_facts.json.

Uses extract_cache/, so after one full run it is instant and needs no API calls.
Results are also written to eval_results.md for a slide.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
os.environ["EVENTS_DB"] = str(Path(tempfile.mkdtemp()) / "eval.db")

import cascade                                    # noqa: E402
from app import parse_sample                      # noqa: E402
from extract import extract_facts                 # noqa: E402
from init_db import get_conn, init_db             # noqa: E402

EXPECTED = json.loads((HERE / "expected_facts.json").read_text())


def matches(spec, value):
    text = str(value).lower()
    if isinstance(spec, dict) and "contains" in spec:
        return all(w.lower() in text for w in spec["contains"])
    if isinstance(spec, dict) and "contains_any" in spec:
        return any(w.lower() in text for w in spec["contains_any"])
    return cascade.same_value(spec, value)


def describe(spec):
    if isinstance(spec, dict):
        words = spec.get("contains") or spec.get("contains_any")
        joiner = " and " if "contains" in spec else " or "
        return "text with " + joiner.join(f"'{w}'" for w in words)
    return str(spec)


def main():
    init_db()
    subprocess.run([sys.executable, str(HERE / "seed.py")], check=True, stdout=subprocess.DEVNULL)
    conn = get_conn()
    event_id = cascade.create_event(conn, "Eval summit", "Corporate Event")
    keys = [r["key"] for r in conn.execute("SELECT key FROM fact_keys")]

    samples = sorted((parse_sample(p) for p in (HERE / "samples").glob("*.txt")),
                     key=lambda s: (s["received_at"] or "", s["file"]))
    totals = {"expected": 0, "found": 0, "false_changes": 0, "quotes": 0, "quotes_ok": 0}
    rows, problems = [], []

    for s in samples:
        spec = EXPECTED.get(s["file"])
        if spec is None:
            problems.append(f"{s['file']}: not in expected_facts.json, skipped")
            continue
        known = {k: f["value"] for k, f in cascade.current_facts(conn, event_id).items()}
        try:
            facts = extract_facts(s["text"], s["kind"], s["received_at"], keys, known)
        except Exception as e:
            problems.append(f"{s['file']}: extraction failed: {e}")
            continue

        found = 0
        for key, want in spec["expected"].items():
            got = [f["value"] for f in facts if f["key"] == key]
            if any(matches(want, v) for v in got):
                found += 1
            else:
                problems.append(f"{s['file']}: {key} expected {describe(want)}, got {got or 'nothing'}")

        false_changes = 0
        for f in facts:
            if f["key"] in spec["expected"] or f["key"] in spec.get("optional", []):
                continue
            if f["key"] in known and cascade.same_value(known[f["key"]], f["value"]):
                continue                          # repeating a value on file is fine
            false_changes += 1
            problems.append(f"{s['file']}: unexpected {f['key']} = {f['value']!r} "
                            f"(quote: {f['quote']!r})")

        flat = " ".join(s["text"].split()).lower()
        quotes_ok = sum(" ".join(f["quote"].split()).lower() in flat for f in facts)

        n = len(spec["expected"])
        totals["expected"] += n
        totals["found"] += found
        totals["false_changes"] += false_changes
        totals["quotes"] += len(facts)
        totals["quotes_ok"] += quotes_ok
        rows.append((s["title"], f"{found}/{n}", false_changes, f"{quotes_ok}/{len(facts)}"))

        # Store the facts and approve every proposal, as a planner would in the demo
        source_id = conn.execute(
            "INSERT INTO sources(event_id, kind, sender, received_at, raw_text) VALUES (?,?,?,?,?)",
            (event_id, s["kind"], s["sender"], s["received_at"], s["text"])).lastrowid
        cascade.record_facts(conn, event_id, source_id, facts)
        for (fact_id,) in conn.execute(
                "SELECT id FROM facts WHERE event_id = ? AND status = 'proposed' ORDER BY id", (event_id,)).fetchall():
            if conn.execute("SELECT status FROM facts WHERE id = ?", (fact_id,)).fetchone()[0] == "proposed":
                cascade.approve(conn, fact_id)
        conn.commit()

    pct = 100 * totals["found"] / totals["expected"] if totals["expected"] else 0
    summary = (f"Facts found: {totals['found']}/{totals['expected']} ({pct:.0f}%)  |  "
               f"False changes: {totals['false_changes']}  |  "
               f"Quotes found word for word in the source: {totals['quotes_ok']}/{totals['quotes']}")

    width = max((len(r[0]) for r in rows), default=10)
    print(f"\n{'Sample':<{width}}  Found   False changes   Quotes verified")
    for title, found, fc, q in rows:
        print(f"{title:<{width}}  {found:<7} {fc:<15} {q}")
    print("\n" + summary)
    if problems:
        print("\nDetails:")
        for p in problems:
            print(" -", p)

    md = ["# Extraction evaluation", "", summary, "",
          "| Sample | Facts found | False changes | Quotes verified |", "|---|---|---|---|"]
    md += [f"| {t} | {f} | {fc} | {q} |" for t, f, fc, q in rows]
    if problems:
        md += ["", "## Details", ""] + [f"- {p}" for p in problems]
    (HERE / "eval_results.md").write_text("\n".join(md) + "\n")
    print("\nWrote eval_results.md")


if __name__ == "__main__":
    main()
