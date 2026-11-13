# Event-orchestrator (Event Cascade Agent)

One change in, every dependent task updated.

The agent reads emails, meeting transcripts, contracts and venue pages, pulls out the
event facts (date, headcount, deadlines, minimums), and keeps each fact with the
sentence it came from. When a new source disagrees with a stored fact, it asks a
person to approve the change, then flags every checklist task that depends on it.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python init_db.py --reset
python seed.py
```

Create a file named `.env` in this folder with your Claude API key (it is gitignored):

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```
python app.py
```

Open http://localhost:8000.

## Demo script

1. **Start.** The venue contract and kickoff email (the samples marked `baseline: yes`)
   are read automatically, so the Event facts window at the top is already filled in.
2. On the **Updates** tab, **Read** the planning call transcript. The agent proposes
   headcount 100 -> 150 and quotes the line from the call. Nothing changes yet.
3. **Approve.** Every task that depends on headcount appears in the right-hand column
   with the reason, plus new items: headcount is over the venue's capacity, and the
   venue needs the new number by the guarantee deadline.
4. Open the **Template** tab: the same tasks are highlighted inside the full checklist.
5. **Read** the date-change email and approve the changes. Due dates shift, the
   winter tasks are flagged as no longer needed and the spring tasks are added.

"Reset demo" clears the event so you can run it again. Extraction results are cached
in `extract_cache/`, so a second run is instant and works offline.

## Files

| File | What it does |
|---|---|
| `Event_orchestrator.xlsx` | Fact keys, task templates and considerations. Edit, then re-run `seed.py` |
| `schema.sql`, `init_db.py`, `seed.py` | SQLite database and seeding |
| `extract.py` | Claude reads a source and returns facts with quotes |
| `cascade.py` | Fact store, change detection, cascade rules (no AI) |
| `app.py`, `static/index.html` | Review screen |
| `samples/` | Demo sources |
| `test_cascade.py` | Checks the cascade without calling Claude: `python test_cascade.py` |
