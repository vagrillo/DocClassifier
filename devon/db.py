"""Connessione al database e schema.

Il database predefinito è ~/.devon/library.db (sovrascrivibile con
l'opzione --db o la variabile d'ambiente DEVON_DB). L'indice full-text
FTS5 è una tabella a "contenuto esterno" puntata su documents: i trigger
documents_ai/au/ad la mantengono allineata automaticamente a ogni
INSERT/UPDATE/DELETE.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    filename  TEXT NOT NULL,
    ext       TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'altro',
    size      INTEGER NOT NULL DEFAULT 0,
    mtime     TEXT NOT NULL DEFAULT '',
    added     TEXT NOT NULL DEFAULT '',
    sha256    TEXT NOT NULL DEFAULT '',
    title     TEXT NOT NULL DEFAULT '',
    author    TEXT NOT NULL DEFAULT '',
    date      TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    text,
    tokenize='unicode61 remove_diacritics 2',
    content='documents',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE OF text ON documents BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS tags (
    tag    TEXT NOT NULL COLLATE NOCASE,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (tag, doc_id)
);

CREATE TABLE IF NOT EXISTS groups (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL COLLATE NOCASE,
    parent_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    query     TEXT            -- NULL = gruppo normale, testo = gruppo smart
);

CREATE TABLE IF NOT EXISTS doc_groups (
    doc_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (doc_id, group_id)
);

CREATE TABLE IF NOT EXISTS concepts (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL COLLATE NOCASE,
    pos_text  TEXT NOT NULL DEFAULT '',
    pos_docs  TEXT NOT NULL DEFAULT '',
    pos_exts  TEXT NOT NULL DEFAULT '',
    neg_text  TEXT NOT NULL DEFAULT '',
    neg_docs  TEXT NOT NULL DEFAULT '',
    neg_exts  TEXT NOT NULL DEFAULT '',
    threshold REAL NOT NULL DEFAULT 0.10,
    created   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sources (
    id         INTEGER PRIMARY KEY,
    path       TEXT UNIQUE NOT NULL,
    added      TEXT NOT NULL DEFAULT '',
    last_scan  TEXT NOT NULL DEFAULT '',
    last_stats TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_documents_sha  ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind);
"""

# Migrazioni successive: applicate solo se la colonna/tabella manca ancora
MIGRATIONS = (
    ("topic", "ALTER TABLE documents ADD COLUMN topic TEXT NOT NULL DEFAULT ''"),
    ("embeddings", (
        "CREATE TABLE IF NOT EXISTS embeddings ("
        "  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,"
        "  chunk  INTEGER NOT NULL DEFAULT 0,"
        "  model  TEXT NOT NULL,"
        "  dim    INTEGER NOT NULL,"
        "  vec    BLOB NOT NULL,"
        "  PRIMARY KEY (doc_id, chunk)"
        ")")),
)

# schema v1 della tabella embeddings (un solo vettore per documento):
# viene convertito in blocco aggiungendo la colonna chunk
EMBEDDINGS_V1_TO_V2 = (
    "CREATE TABLE IF NOT EXISTS embeddings_v2 ("
    "  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,"
    "  chunk  INTEGER NOT NULL DEFAULT 0,"
    "  model  TEXT NOT NULL,"
    "  dim    INTEGER NOT NULL,"
    "  vec    BLOB NOT NULL,"
    "  PRIMARY KEY (doc_id, chunk)"
    ")")


def default_db_path() -> Path:
    env = os.environ.get("DEVON_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".devon" / "library.db"


_SCHEMA_CHECKED: set[str] = set()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Apre (creando se serve) il database e ne verifica lo schema.

    La verifica DDL costa una transazione di scrittura: su questo stesso
    file può collidere con un import in corso. Si esegue quindi una
    sola volta per processo (il caso multi-processo resta coperto dal
    fatto che lo schema, una volta creato, è già a posto).
    """
    path = Path(db_path).expanduser() if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve()) if path.exists() else str(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")  # tollera lavori concorrenti
    if key not in _SCHEMA_CHECKED:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        for name, ddl in MIGRATIONS:
            if name == "embeddings":
                conn.executescript(ddl)
                ecols = {r["name"] for r in
                         conn.execute("PRAGMA table_info(embeddings)")}
                if "chunk" not in ecols:
                    # v1 (un vettore per documento) -> v2 (blocchi per
                    # documento); si saltano le eventuali righe orfane
                    conn.executescript(EMBEDDINGS_V1_TO_V2)
                    conn.execute(
                        "INSERT OR IGNORE INTO embeddings_v2 "
                        "(doc_id, chunk, model, dim, vec) "
                        "SELECT doc_id, 0, model, dim, vec FROM embeddings "
                        "WHERE doc_id IN (SELECT id FROM documents)")
                    conn.execute("DROP TABLE embeddings")
                    conn.execute("ALTER TABLE embeddings_v2 "
                                 "RENAME TO embeddings")
            elif name not in cols:
                conn.execute(ddl)
                cols.add(name)
        # concetti: modalità d'uso delle tipologie ('bonus' o 'solo')
        ccols = {r["name"] for r in conn.execute("PRAGMA table_info(concepts)")}
        if "ext_mode" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN ext_mode "
                         "TEXT NOT NULL DEFAULT 'bonus'")
        if "members" not in ccols:
            # cache del numero di documenti classificati: evita di
            # riclassificare tutto solo per mostrare il conteggio
            conn.execute("ALTER TABLE concepts ADD COLUMN members INTEGER")
        _SCHEMA_CHECKED.add(key)
    return conn
