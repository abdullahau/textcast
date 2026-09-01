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
    archived      INTEGER NOT NULL DEFAULT 0,
    -- How to build THIS article. Chosen when it is added, editable after.
    build_options TEXT    NOT NULL DEFAULT '{}'
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
    -- AUTOINCREMENT so a re-queued job never reuses the id a client is polling.
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Tags replace the newsletter section: one flat, user-controlled way to
-- group and filter. A detected newsletter simply becomes a tag like any other.
CREATE TABLE IF NOT EXISTS tag (
    name     TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_tag (
    article_id INTEGER NOT NULL REFERENCES article (id) ON DELETE CASCADE,
    tag        TEXT    NOT NULL REFERENCES tag (name) ON DELETE CASCADE,
    PRIMARY KEY (article_id, tag)
);

CREATE INDEX IF NOT EXISTS article_tag_by_tag ON article_tag (tag, article_id);

-- How to say things. Each row rewrites matching text on the way to the
-- engine: either into different words, or into IPA phonemes that Kokoro
-- takes verbatim.
CREATE TABLE IF NOT EXISTS pronunciation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    -- word: whole word. phrase: literal substring. regex: a raw pattern.
    kind         TEXT    NOT NULL DEFAULT 'word',
    pattern      TEXT    NOT NULL,
    replacement  TEXT    NOT NULL,
    -- 1 when the replacement is IPA, wrapped as [match](/ipa/) for misaki.
    is_phonemes  INTEGER NOT NULL DEFAULT 0,
    ignore_case  INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    note         TEXT    NOT NULL DEFAULT '',
    -- Lower runs first, so structural rules beat word-level ones.
    sort_order   INTEGER NOT NULL DEFAULT 100,
    builtin      INTEGER NOT NULL DEFAULT 0,
    added_at     TEXT    NOT NULL,
    UNIQUE (kind, pattern)
);

CREATE INDEX IF NOT EXISTS pronunciation_order ON pronunciation (enabled, sort_order, id);
