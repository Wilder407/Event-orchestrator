-- Event Cascade Agent schema (SQLite)

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL            -- reception, conference, workshop
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    kind        TEXT NOT NULL,           -- email, transcript, contract, web
    sender      TEXT,
    received_at TEXT NOT NULL,           -- ISO 8601 timestamp
    raw_text    TEXT NOT NULL
);

-- One row per observation; changes add rows instead of overwriting
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    key         TEXT NOT NULL,           -- e.g. headcount_expected
    value       TEXT NOT NULL,
    unit        TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','confirmed','superseded','conflict')),
    source_id   INTEGER REFERENCES sources(id),
    quote       TEXT,                    -- exact text the fact came from
    confidence  REAL,
    observed_at TEXT NOT NULL
);

-- The fact keys the extractor is allowed to produce
CREATE TABLE IF NOT EXISTS fact_keys (
    key         TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS task_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT UNIQUE NOT NULL,    -- C00001, P00001 from the spreadsheet
    event_type  TEXT NOT NULL,           -- Corporate Event, Personal Event
    stage       TEXT,                    -- Planning, Executing
    owner_role  TEXT,                    -- Event Planner, Marketing, ...
    title       TEXT NOT NULL,
    notes       TEXT,
    depends_on  TEXT NOT NULL,           -- JSON list of fact keys
    due_offset_days INTEGER              -- negative = days before event_start_date
);

CREATE TABLE IF NOT EXISTS considerations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    triggers    TEXT NOT NULL,           -- JSON, e.g. {"on_change":["event_start_date"],"condition":"season_changed"}
    action      TEXT,                    -- e.g. Alert event planner to review
    origin_event TEXT,                   -- where the lesson came from
    tasks       TEXT NOT NULL DEFAULT '[]'  -- JSON list of task titles it adds
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    template_id INTEGER REFERENCES task_templates(id),
    consideration_id INTEGER REFERENCES considerations(id),
    title       TEXT NOT NULL,
    due_date    TEXT,
    status      TEXT NOT NULL DEFAULT 'ok'
                CHECK (status IN ('ok','needs_recheck','done','dismissed')),
    dismiss_reason TEXT,
    asana_gid   TEXT
);

-- Current value of each fact = latest non-superseded row
CREATE VIEW IF NOT EXISTS current_facts AS
SELECT f.* FROM facts f
WHERE f.status != 'superseded'
  AND f.observed_at = (
      SELECT MAX(observed_at) FROM facts
      WHERE event_id = f.event_id AND key = f.key AND status != 'superseded'
  );
