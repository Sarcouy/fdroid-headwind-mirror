CREATE TABLE tracked_package (
    pkg                       TEXT PRIMARY KEY,
    hmdm_application_id       INTEGER,
    repo_url                  TEXT    NOT NULL,
    expected_signer           TEXT,
    mirror                    INTEGER NOT NULL DEFAULT 1,
    auto_approve              INTEGER NOT NULL DEFAULT 0,
    paused                    INTEGER NOT NULL DEFAULT 0,
    last_seen_version_code    INTEGER,
    last_created_version_code INTEGER,
    last_pushed_version_code  INTEGER,
    created_at                TEXT    NOT NULL,
    updated_at                TEXT    NOT NULL
);

CREATE TABLE sync_run (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    status           TEXT    NOT NULL,
    packages_checked INTEGER NOT NULL DEFAULT 0,
    versions_created INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sync_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES sync_run (id) ON DELETE CASCADE,
    pkg          TEXT,
    level        TEXT    NOT NULL,
    code         TEXT    NOT NULL,
    message      TEXT    NOT NULL,
    payload_json TEXT,
    created_at   TEXT    NOT NULL
);

CREATE INDEX idx_sync_event_run ON sync_event (run_id);
CREATE INDEX idx_sync_event_pkg ON sync_event (pkg);
