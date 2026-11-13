"""Pull event facts out of an email, transcript, contract or web page with Claude.

Needs an API key: put  ANTHROPIC_API_KEY=sk-ant-...  in a file named .env next to
this script (it is gitignored), or export it in your shell.

Results are cached in extract_cache/ by source text, so re-running the demo is
instant and still works if the wifi drops.
"""
import hashlib
import json
import os
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
CACHE_DIR = HERE / "extract_cache"
MODEL = "claude-opus-5-5"

SYSTEM = """You extract planning facts about one event from a source document \
(an email, a meeting transcript, a contract or a venue web page). The facts feed a \
checklist that event planners rely on, so a wrong fact is worse than a missing one.

Only report a fact the document actually states or decides for this event. Skip \
ideas that were floated and dropped, questions, and examples about other events. \
If the document revises a value part-way through, report the final value.

Format values so they can be compared across documents:
- dates as YYYY-MM-DD and times as 24-hour HH:MM (the document's received date is \
given so you can resolve phrases like "next Thursday")
- counts and money as bare digits with no commas or symbols; put what they measure \
in unit ("guests", "USD", "%", "minutes")
- everything else as a short plain phrase

Keys that are easy to confuse:
- event_end_date: only when the event ends on a later date than it starts. Leave \
it out for a single-day event.
- headcount_expected is how many guests are being planned for. headcount_guaranteed \
is the number formally committed to the venue or caterer. headcount_guarantee_deadline \
is the date that commitment is due.
- deposit_due_date is when a deposit must be paid; cancellation_deadline is the last \
date to cancel without penalty.

You may be given the values already on file for this event. When the document says \
the same thing as a value on file, in any wording or level of detail, return the \
value on file exactly as written so it is not mistaken for a change. Return a \
different value only when the document really disagrees with or adds to it.

quote must be copied word for word from the document: the shortest passage that \
proves the fact. confidence is 0 to 1; use below 0.7 when the document is tentative \
or you had to infer the value."""


def _load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _schema(fact_keys):
    return {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": sorted(fact_keys)},
                        "value": {"type": "string"},
                        "unit": {"type": "string"},
                        "quote": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["key", "value", "unit", "quote", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["facts"],
        "additionalProperties": False,
    }


def extract_facts(raw_text, kind, received_at, fact_keys, known=None, use_cache=True):
    """Return a list of {key, value, unit, quote, confidence} dicts.

    known: {fact key: value on file}, so reworded repeats aren't reported as changes.
    """
    on_file = "".join(f"\n{k}: {v}" for k, v in sorted((known or {}).items()))
    digest = hashlib.sha256(
        f"{MODEL}|{SYSTEM}|{kind}|{received_at}|{sorted(fact_keys)}|{raw_text}|{on_file}".encode()).hexdigest()[:20]
    cache_file = CACHE_DIR / f"{digest}.json"
    if use_cache and cache_file.exists():
        return json.loads(cache_file.read_text())

    _load_env()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError("No Claude API key found. Add a line ANTHROPIC_API_KEY=sk-ant-... "
                           "to a file named .env in the project folder, then try again")
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(fact_keys)}},
        # if the model declines a request, the API retries it on a fallback model
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{
            "role": "user",
            "content": (f"Document type: {kind}\nReceived: {received_at}\n\n<document>\n{raw_text}\n</document>"
                        + (f"\n\n<values_on_file>{on_file}\n</values_on_file>" if on_file else "")),
        }],
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Extraction did not finish (stop_reason={response.stop_reason})")
    facts = json.loads(next(b.text for b in response.content if b.type == "text"))["facts"]

    # A quote that isn't in the document can't be trusted: send it to human review.
    flat = " ".join(raw_text.split()).lower()
    for f in facts:
        if " ".join(f["quote"].split()).lower() not in flat:
            f["confidence"] = min(f["confidence"], 0.5)

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(facts, indent=2))
    return facts
