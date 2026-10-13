"""Suggerimenti "Vedi anche" (come l'AI di DEVONthink, in versione semplice).

Per ogni documento si costruisce un vettore TF-IDF sulle parole del suo
testo (escludendo le stopword italiane e inglesi più comuni) e si
ordinano gli altri documenti per similarità del coseno. È puro Python:
funziona bene fino a qualche decina di migliaia di documenti.
"""

from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from collections import Counter

TOKEN_RE = re.compile(r"[a-z0-9]+")

STOPWORDS = {
    # italiano
    "che", "di", "il", "la", "le", "lo", "gli", "un", "una", "uno", "del",
    "della", "dei", "delle", "dello", "degli", "al", "alla", "allo", "ai",
    "agli", "alle", "nel", "nella", "nelle", "nel", "con", "su", "per",
    "tra", "fra", "non", "sono", "essere", "sia", "come", "più", "piu",
    "anche", "questo", "questa", "questi", "queste", "quello", "quella",
    "se", "si", "ha", "hanno", "era", "già", "gia", "ma", "o", "ed", "in",
    "da", "at", "noi", "voi", "loro", "suo", "sua", "più", "molto", "poi",
    # inglese
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
    "how", "man", "new", "now", "old", "see", "two", "way", "who", "did",
    "its", "let", "she", "too", "use", "with", "this", "that", "have",
    "from", "they", "will", "would", "there", "their", "what", "about",
    "which", "when", "your", "said", "each", "make", "like", "just",
    "than", "then", "them", "these", "some", "into", "only", "over",
    "such", "most", "very", "upon", "shall", "unto", "due", "via",
}


def strip_diacritics(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    low = strip_diacritics(text).lower()
    return [t for t in TOKEN_RE.findall(low)
            if len(t) >= 3 and t not in STOPWORDS]


def similar_documents(
    conn: sqlite3.Connection,
    doc_id: int,
    limit: int = 10,
    max_docs: int = 200_000,
) -> list[dict]:
    """Ritorna i documenti più affini, con chiave `score` (0..1)."""
    rows = conn.execute(
        "SELECT id, title, path, kind, filename, text FROM documents "
        "WHERE text != '' ORDER BY id LIMIT ?", (max_docs,)).fetchall()
    if len(rows) < 2:
        return []

    counts = {}
    for r in rows:
        counts[r["id"]] = Counter(tokenize(r["text"]))
    if doc_id not in counts:
        return []  # documento nuovo o oltre il limite: niente confronto

    doc_freq: Counter = Counter()
    for c in counts.values():
        doc_freq.update(c.keys())
    n_docs = len(rows)
    idf = {t: math.log((n_docs + 1) / (df + 1)) + 1.0
           for t, df in doc_freq.items()}

    def vector(doc_id: int) -> dict[str, float]:
        return {t: (1 + math.log(f)) * idf[t]
                for t, f in counts[doc_id].items()}

    target = vector(doc_id)
    if not target:
        return []
    tnorm = math.sqrt(sum(v * v for v in target.values()))

    out = []
    for r in rows:
        rid = r["id"]
        if rid == doc_id:
            continue
        vec = vector(rid)
        dot = sum(w * target.get(t, 0.0) for t, w in vec.items())
        vnorm = math.sqrt(sum(v * v for v in vec.values()))
        if dot <= 0 or tnorm == 0 or vnorm == 0:
            continue
        score = dot / (tnorm * vnorm)
        d = dict(r)
        d.pop("text", None)
        d["score"] = round(score, 4)
        out.append(d)

    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:limit]
