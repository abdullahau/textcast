-- textcast storage.
--
-- The block table is the centre: reading, listening, highlighting, seeking and
-- search all read from it, so they cannot drift apart.

CREATE TABLE IF NOT EXISTS article (
    id            INTEGER PRIMARY KEY,
    slug          TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    subtitle      TEXT    NOT NULL DEFAULT '',
    author        TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',
    series        TEXT,
    url           TEXT    NOT NULL DEFAULT '',
    lang          TEXT    NOT NULL DEFAULT 'en',
    fingerprint   TEXT    NOT NULL DEFAULT '',
    adapter       TEXT    NOT NULL DEFAULT '',
    published_at  TEXT,
    added_at      TEXT    NOT NULL,
    word_count    INTEGER NOT NULL DEFAULT 0,
    -- new -> queued -> building -> ready, or failed
    status        TEXT    NOT NULL DEFAULT 'new',
    audio_ms      INTEGER NOT NULL DEFAULT 0,
    audio_bytes   INTEGER NOT NULL DEFAULT 0,
    engine        TEXT    NOT NULL DEFAULT '',
    voice         TEXT    NOT NULL DEFAULT '',
    starred       INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS article_added   ON article (added_at DESC);
CREATE INDEX IF NOT EXISTS article_series  ON article (series, published_at DESC);
CREATE INDEX IF NOT EXISTS article_status  ON article (status);
CREATE UNIQUE INDEX IF NOT EXISTS article_fingerprint ON article (fingerprint)
    WHERE fingerprint <> '';

CREATE TABLE IF NOT EXISTS section (
    article_id  INTEGER NOT NULL REFERENCES article (id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    summary     TEXT,
    file        TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, idx)
);

CREATE TABLE IF NOT EXISTS block (
    id           INTEGER PRIMARY KEY,
    article_id   INTEGER NOT NULL REFERENCES article (id) ON DELETE CASCADE,
    section_idx  INTEGER NOT NULL,
    idx          INTEGER NOT NULL,
    block_id     TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    footnote_ref TEXT,
    -- Filled in once audio exists; start_ms is relative to the section file.
    start_ms     INTEGER,
    dur_ms       INTEGER,
    speech_ms    INTEGER,
    UNIQUE (article_id, block_id)
);

CREATE INDEX IF NOT EXISTS block_order ON block (article_id, section_idx, idx);

CREATE VIRTUAL TABLE IF NOT EXISTS block_fts
    USING fts5 (text, content='block', content_rowid='id', tokenize='porter unicode61');

CREATE TRIGGER IF NOT EXISTS block_ai AFTER INSERT ON block BEGIN
    INSERT INTO block_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS block_ad AFTER DELETE ON block BEGIN
    INSERT INTO block_fts (block_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS block_au AFTER UPDATE OF text ON block BEGIN
    INSERT INTO block_fts (block_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO block_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS job (
    id          INTEGER PRIMARY KEY,
    article_id  INTEGER REFERENCES article (id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'build',
    -- queued -> running -> done, or failed / cancelled
    state       TEXT    NOT NULL DEFAULT 'queued',
    progress    REAL    NOT NULL DEFAULT 0.0,
    message     TEXT    NOT NULL DEFAULT '',
    error       TEXT,
    options     TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS job_state ON job (state, created_at);

-- Playback position, so the laptop resumes where the phone stopped.
CREATE TABLE IF NOT EXISTS position (
    article_id  INTEGER PRIMARY KEY REFERENCES article (id) ON DELETE CASCADE,
    section_idx INTEGER NOT NULL DEFAULT 0,
    ms          INTEGER NOT NULL DEFAULT 0,
    finished    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL
);

-- Per-newsletter defaults: voice, whether to auto-build on arrival.
CREATE TABLE IF NOT EXISTS series (
    name        TEXT PRIMARY KEY,
    display     TEXT NOT NULL DEFAULT '',
    voice       TEXT NOT NULL DEFAULT '',
    quote_voice TEXT NOT NULL DEFAULT '',
    auto_build  INTEGER NOT NULL DEFAULT 1,
    skip_footnotes INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
