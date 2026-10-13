"""Ricerca full-text con filtri e snippet evidenziati.

Sintassi di query accettata (i filtri si possono combinare e mescolare
al testo libero):

    parole da cercare            ricerca full-text (AND tra le parole)
    prodot*                      ricerca per prefisso
    -parola                      esclude i documenti con quella parola
    tag:personale                solo documenti con questo tag
    kind:pdf  ext:.docx          filtro per tipo / estensione
    path:consiglio               filtro per percorso
    group:personale              documento dentro un gruppo (o suo figlio)

Le parole vengono passate a FTS5 come frasi quotate, quindi i caratteri
speciali della sintassi FTS5 non possono causare errori di parsing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

SNIPPET_PRE = "≪"
SNIPPET_POST = "≫"
SNIPPET_OMIT = "…"


@dataclass
class ParsedQuery:
    terms: list[str] = field(default_factory=list)      # incluse
    excluded: list[str] = field(default_factory=list)   # escluse (-parola)
    tag: str | None = None
    kind: str | None = None
    ext: str | None = None
    path: str | None = None
    group: str | None = None
    topic: str | None = None

    def match_sql(self) -> str:
        """Costruisce la clausola MATCH per FTS5 (mai errori di sintassi)."""
        parts = []
        for t in self.terms:
            prefix = t.endswith("*")
            clean = t.rstrip("*").replace('"', '""')
            parts.append(f'"{clean}"*' if prefix else f'"{clean}"')
        if not parts:
            return ""
        return " AND ".join(parts)


def parse_query(raw: str) -> ParsedQuery:
    q = ParsedQuery()
    try:
        import shlex
        tokens = shlex.split(raw)  # gestisce valori con spazi: topic:"01 · x"
    except ValueError:
        tokens = raw.split()
    for token in tokens:
        low = token.lower()
        if low.startswith("tag:") and len(low) > 4:
            q.tag = low[4:]
        elif low.startswith("kind:") and len(low) > 5:
            q.kind = low[5:]
        elif low.startswith("ext:") and len(low) > 4:
            q.ext = low[4:].lstrip(".")
        elif low.startswith("path:") and len(low) > 5:
            q.path = low[5:]
        elif low.startswith("group:") and len(low) > 6:
            q.group = low[6:]
        elif low.startswith("topic:") and len(low) > 6:
            q.topic = low[6:]
        elif low.startswith("-") and len(low) > 1:
            q.excluded.append(low[1:])
        else:
            q.terms.append(token)
    return q


def group_subtree_ids(conn: sqlite3.Connection, name: str) -> list[int]:
    """Id del gruppo chiamato `name` e di tutti i suoi discendenti."""
    rows = conn.execute(
        "WITH RECURSIVE sub(id) AS ("
        "  SELECT id FROM groups WHERE name = ?"
        "  UNION ALL"
        "  SELECT g.id FROM groups g JOIN sub s ON g.parent_id = s.id"
        ") SELECT id FROM sub", (name,)).fetchall()
    return [r["id"] for r in rows]


def build_where(parsed: ParsedQuery) -> tuple[str, list]:
    """Clausole WHERE aggiuntive (fuori dal MATCH) e parametri relativi."""
    clauses, params = [], []
    if parsed.tag:
        clauses.append(
            "d.id IN (SELECT doc_id FROM tags WHERE tag = ? COLLATE NOCASE)")
        params.append(parsed.tag)
    if parsed.kind:
        clauses.append("d.kind LIKE ? COLLATE NOCASE")
        params.append(f"%{parsed.kind}%")
    if parsed.ext:
        clauses.append("d.ext = ? COLLATE NOCASE")
        params.append(f".{parsed.ext.lstrip('.')}")
    if parsed.path:
        clauses.append("d.path LIKE ? COLLATE NOCASE")
        params.append(f"%{parsed.path}%")
    if parsed.topic:
        clauses.append("d.topic LIKE ? COLLATE NOCASE")
        params.append(f"%{parsed.topic}%")
    # le esclusioni (-parola) non usano la grammatica FTS5 ("a AND NOT b"
    # sarebbe un errore di sintassi) ma una sottoquery: funziona sempre
    for t in parsed.excluded:
        clean = t.replace('"', '""')
        clauses.append(
            "d.id NOT IN (SELECT rowid FROM docs_fts "
            "WHERE docs_fts MATCH ?)")
        params.append(f'"{clean}"')
    return " AND ".join(clauses), params


def search(
    conn: sqlite3.Connection,
    raw_query: str,
    limit: int = 30,
) -> list[sqlite3.Row]:
    """Cerca e ritorna righe con metadati, rank e snippet evidenziato."""
    parsed = parse_query(raw_query)
    match_sql = parsed.match_sql()

    extra_where, extra_params = build_where(parsed)
    if parsed.group:
        ids = group_subtree_ids(conn, parsed.group)
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        extra_where = (extra_where + " AND " if extra_where else "") + \
            f"d.id IN (SELECT doc_id FROM doc_groups WHERE group_id IN ({ph}))"
        extra_params = extra_params + ids

    if not match_sql and not extra_where:
        return []  # query vuota

    # snippet() e bm25() richiedono una clausola MATCH: senza testo da
    # cercare si usano colonne fittizie e un ordinamento normale
    if match_sql:
        select_tail = ("bm25(docs_fts) AS rank, "
                       "snippet(docs_fts, 0, ?, ?, ?, 14) AS snippet ")
        params = [SNIPPET_PRE, SNIPPET_POST, SNIPPET_OMIT]
        order = "ORDER BY rank "
    else:
        select_tail = "0 AS rank, '' AS snippet "
        params = []
        order = "ORDER BY d.date DESC, d.id DESC "

    sql = (
        "SELECT d.id, d.path, d.filename, d.title, d.kind, d.ext, d.size, "
        "d.date, d.author, " + select_tail +
        "FROM documents d JOIN docs_fts ON docs_fts.rowid = d.id "
    )
    conds = []
    if match_sql:
        conds.append("docs_fts MATCH ?")
        params.append(match_sql)
    if extra_where:
        conds.append(extra_where)
        params.extend(extra_params)
    sql += "WHERE " + " AND ".join(conds) + " " + order + "LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()
