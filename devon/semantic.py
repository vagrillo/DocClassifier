"""Ricerca semantica e classificazione automatica per contenuti.

Due livelli, con la stessa interfaccia:

1. <b>Embedding neurali</b> (consigliato, opzionale): con
   `pip install fastembed` ogni documento viene rappresentato da un
   vettore denso (modello multilingue locale, ~120 MB scaricati una
   volta sola); la ricerca confronta il significato della query con il
   significato del documento ("rendiconto finanziario" trova anche
   "bilancio d'esercizio"). Clustering e suggerimenti usano gli stessi
   vettori.

2. <b>Fallback lessicale</b> (sempre disponibile, sola libreria
   standard): la query viene confrontata con i vettori TF-IDF dei
   documenti — simile alla ricerca per parole ma con rango a similarità
   vettoriale e re-ranking pseudo-relevance, quindi tollera parole
   diverse nello stesso argomento.

Tutto il calcolo avviene in locale; nessun dato lascia il computer.
"""

from __future__ import annotations

import math
import random
import re
import sqlite3
import struct
from collections import Counter, defaultdict

from . import search as searchm
from .similar import tokenize

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_CHUNK = 16  # testi per inferenza (il modello è batch)
# i modelli e5 richiedono i prefissi query:/passage:, gli altri no
_USE_PREFIX = "e5" in MODEL_NAME.lower()

# Embedding a blocchi: il modello vede ~128 token per volta, quindi un
# documento lungo viene spezzato in passaggi da ~1000 caratteri (con un
# piccolo overlap per non tagliare le frasi) e OGNI blocco viene
# vettorizzato. La ricerca e la similarità aggregano per documento con
# il massimo: un documento risulta affine se lo è una sua parte.
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 80
CHUNK_FULL = 1200  # testi più corti sono già coperti da un solo vettore

# tipi considerati "documenti" per il raggruppamento: su un disco pieno
# di file di sviluppo, clusterizzare tutto producerebbe gruppi come
# «license · string · android» invece che argomenti documentali
DOC_KINDS = ("PDF", "Word", "Excel", "PowerPoint", "email", "pagina web",
             "e-book")


def embeddings_status() -> tuple[bool, str]:
    """(disponibile, messaggio) della libreria degli embedding."""
    try:
        import fastembed  # noqa: F401
        return True, ""
    except ImportError:
        return False, ("embedding non installati: pip3 install fastembed "
                       "(fallback lessicale attivo)")


# ------------------------------------------------------------------ tfidf --

def tfidf_vectors(conn: sqlite3.Connection,
                  max_docs: int = 100_000) -> tuple[dict[int, dict[str, float]],
                                                    dict[str, float]]:
    """Vettori TF-IDF sparsi di tutti i documenti con testo + idf."""
    rows = conn.execute(
        "SELECT id, text FROM documents WHERE text != '' ORDER BY id LIMIT ?",
        (max_docs,)).fetchall()
    counts = {r["id"]: Counter(tokenize(r["text"])) for r in rows}
    doc_freq: Counter = Counter()
    for c in counts.values():
        doc_freq.update(c.keys())
    n = max(len(rows), 1)
    idf = {t: math.log((n + 1) / (df + 1)) + 1.0 for t, df in doc_freq.items()}

    def vec(c: Counter) -> dict[str, float]:
        return {t: (1 + math.log(f)) * idf[t] for t, f in c.items()}

    out = {i: vec(c) for i, c in counts.items()}
    return out, idf


def _normalize(v: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {t: x / norm for t, x in v.items()}


def _cosine_sparse(a: dict[str, float], b: dict[str, float]) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


# ------------------------------------------------------------- embedding --

_MODEL_CACHE: dict = {"model": None}


def _get_model():
    # il caricamento della sessione ONNX costa secondi: il modello va
    # tenuto in memoria per tutto il processo
    if _MODEL_CACHE["model"] is not None:
        return _MODEL_CACHE["model"]
    from fastembed import TextEmbedding
    try:
        model = TextEmbedding(model_name=MODEL_NAME)
    except Exception:
        model = TextEmbedding(model_name=MODEL_NAME,
                              providers=["CPUExecutionProvider"])
    _MODEL_CACHE["model"] = model
    return model


def chunk_text(text: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Spezza un testo in passaggi di ~`size` caratteri, cercando di non
    tagliare le parole (con un piccolo overlap tra blocchi consecutivi)."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind(" ", end - 60, end)
            if cut > start + size // 2:
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def embed_all(conn: sqlite3.Connection, progress: dict | None = None) -> dict:
    """Calcola e salva gli embedding (a blocchi) dei documenti con testo.

    Regole di copertura del contenuto:
    - tipi documento (PDF, Office, email, pagine web...): spezzati in
      blocchi, TUTTO il contenuto viene vettorizzato;
    - altri file (codice, licenze, file indicizzati col solo nome):
      un solo vettore sull'inizio del testo;
    - i documenti già coperti secondo queste regole vengono saltati.
    """
    stats = progress if progress is not None else {}
    stats.update({"done": 0, "total": 0, "fatto": False, "errore": None,
                  "modello": MODEL_NAME})
    try:
        model = _get_model()
    except Exception as exc:
        stats["errore"] = f"fastembed non disponibile: {exc}"
        stats["fatto"] = True
        return stats

    rows = conn.execute(
        "SELECT d.id, d.text, d.kind, d.kind IN (%s) AS is_doc, "
        "(SELECT COUNT(*) FROM embeddings e WHERE e.doc_id = d.id) AS nv "
        "FROM documents d WHERE d.text != ''"
        % ",".join("?" * len(DOC_KINDS)), DOC_KINDS).fetchall()
    work: list[tuple[int, list[str]]] = []
    for r in rows:
        text, is_doc, nv = r["text"], r["is_doc"], r["nv"]
        if is_doc:
            need = nv == 0 or (nv == 1 and len(text) > CHUNK_FULL)
            pieces = chunk_text(text) if need else None
        else:
            # non documento: un vettore sull'inizio basta
            if nv > 0 or not text.strip():
                continue
            pieces = [text[:4000]]
        if pieces is None or not pieces:
            continue
        work.append((r["id"], pieces))
    stats["total"] = sum(len(p) for _, p in work)
    try:
        prefix = "passage: " if _USE_PREFIX else ""
        done_chunks = 0
        batch: list[tuple[int, int, str]] = []  # (doc_id, chunk_idx, testo)
        for doc_id, chunks in work:
            conn.execute("DELETE FROM embeddings WHERE doc_id = ?", (doc_id,))
            for idx, piece in enumerate(chunks):
                batch.append((doc_id, idx, prefix + piece[:4000]))
                if len(batch) >= EMBED_CHUNK:
                    done_chunks = _flush(conn, model, batch, done_chunks,
                                         stats)
                    batch = []
        if batch:
            done_chunks = _flush(conn, model, batch, done_chunks, stats)
        conn.commit()
    except Exception as exc:
        stats["errore"] = str(exc)
    stats["fatto"] = True
    return stats


def _flush(conn, model, batch, done_chunks, stats) -> int:
    vectors = list(model.embed([t for _, _, t in batch]))
    for (doc_id, idx, _), vec in zip(batch, vectors):
        dim = len(vec)
        conn.execute(
            "INSERT OR REPLACE INTO embeddings(doc_id, chunk, model, dim, vec) "
            "VALUES(?,?,?,?,?)",
            (doc_id, idx, MODEL_NAME, dim,
             struct.pack(f"{dim}f", *[float(x) for x in vec])))
    done_chunks += len(batch)
    stats["done"] = done_chunks
    if done_chunks % (EMBED_CHUNK * 20) == 0:
        conn.commit()
    return done_chunks


def _np():
    try:
        import numpy
        return numpy
    except ImportError:
        return None


_MAT_CACHE: dict = {"n": -1, "data": None}


def _load_embeddings_np(conn: sqlite3.Connection):
    """Matrice dei blocchi embedding, normalizzata, con cache.

    Ritorna un dizionario numpy:
      row_doc  (N,)  documento di ogni riga-blocco
      mat      (N,d) blocchi normalizzati
      doc_ids  (D,)  documenti (ordinati, uno per gruppo di righe)
      starts   (D,)  riga di inizio di ogni documento (per reduceat)
    oppure None se non ci sono embedding o numpy manca.
    """
    n = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    if n == 0 or _np() is None:
        return None
    if _MAT_CACHE["n"] == n and _MAT_CACHE["data"] is not None:
        return _MAT_CACHE["data"]
    import numpy as np
    rows = conn.execute(
        "SELECT e.doc_id, e.vec, LENGTH(d.text) AS tl FROM embeddings e "
        "JOIN documents d ON d.id = e.doc_id "
        "ORDER BY e.doc_id, e.chunk").fetchall()
    if not rows:
        return None
    dim = len(rows[0]["vec"]) // 4
    mat = np.empty((len(rows), dim), dtype=np.float32)
    row_doc = np.empty(len(rows), dtype=np.int64)
    tl = np.empty(len(rows), dtype=np.int64)
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r["vec"], dtype=np.float32)
        row_doc[i] = r["doc_id"]
        tl[i] = r["tl"] or 0
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    doc_ids, starts = np.unique(row_doc, return_index=True)
    # i documenti col solo nome file (testo ~ filename) generano vettori
    # rumorosi: esclusi dal rango semantico, restano trovabili via FTS
    real = tl >= 100
    doc_real = np.maximum.reduceat(real.astype(np.int64), starts) > 0
    data = {"row_doc": row_doc, "mat": mat,
            "doc_ids": doc_ids, "starts": starts, "doc_real": doc_real}
    _MAT_CACHE.update(n=n, data=data)
    return data


def embed_query(query: str):
    model = _get_model()
    prefix = "query: " if _USE_PREFIX else ""
    return list(model.embed([prefix + query]))[0]


# ---------------------------------------------------------- ricerca sem. --

def semantic_search(conn: sqlite3.Connection, query: str,
                    limit: int = 30) -> list[dict]:
    """Cerca per significato, rispettando i filtri della query.

    I filtri (kind:, tag:, ext:, path:, group:, topic:) restringono i
    candidati; solo le parole libere vengono vettorizzate. Embedding se
    presenti, fallback TF-IDF altrimenti.
    """
    parsed = searchm.parse_query(query)
    free = " ".join(parsed.terms)
    allowed = _filter_ids(conn, parsed)

    if free:
        results = _semantic_rank(conn, free, limit, allowed)
    elif allowed:
        # solo filtri, niente testo: elenco per data (come la ricerca)
        results = []
        for r in conn.execute(
                "SELECT id, path, filename, title, kind, ext, size, date, "
                "author FROM documents WHERE id IN (%s) "
                "ORDER BY date DESC, id DESC LIMIT ?"
                % ",".join("?" * len(allowed)),
                sorted(allowed)[:1000] + [limit]):
            results.append(dict(r))
    else:
        results = []
    return results


def _filter_ids(conn, parsed) -> set | None:
    """Insieme degli id ammessi dai filtri (None = nessun filtro)."""
    has_filters = any([parsed.tag, parsed.kind, parsed.ext, parsed.path,
                       parsed.group, parsed.topic])
    if not has_filters:
        return None
    where, params = searchm.build_where(parsed)
    if parsed.group:
        gids = searchm.group_subtree_ids(conn, parsed.group)
        if not gids:
            return set()
        ph = ",".join("?" * len(gids))
        where = (where + " AND " if where else "") + \
            f"id IN (SELECT doc_id FROM doc_groups WHERE group_id IN ({ph}))"
        params = params + gids
    sql = "SELECT id FROM documents d"
    if where:
        sql += " WHERE " + where
    return {r["id"] for r in conn.execute(sql, params)}


def _semantic_rank(conn, free: str, limit: int,
                   allowed: set | None) -> list[dict]:
    data = _load_embeddings_np(conn)
    if data is not None and embeddings_status()[0]:
        import numpy as np
        q = np.array(embed_query(free), dtype=np.float32)
        norm = float(np.linalg.norm(q)) or 1.0
        q = q / norm
        sims = data["mat"] @ q
        # massimo per documento: basta che UN blocco sia affine;
        # i documenti col solo nome file non partecipano al rango
        doc_sims = np.maximum.reduceat(sims, data["starts"])
        doc_sims[~data["doc_real"]] = -1.0
        order = np.argsort(-doc_sims)
        out_docs = []
        for j in order:
            did = int(data["doc_ids"][j])
            if allowed is not None and did not in allowed:
                continue
            if doc_sims[j] <= 0.05:
                break
            out_docs.append(did)
            if len(out_docs) >= limit:
                break
        scores = {int(data["doc_ids"][j]): float(doc_sims[j]) for j in order}
        out = []
        for r in conn.execute(
                "SELECT id, path, filename, title, kind, ext, size, date, "
                "author FROM documents WHERE id IN (%s)"
                % ",".join("?" * len(out_docs)), out_docs):
            d = dict(r)
            d["score"] = round(scores.get(r["id"], 0.0), 4)
            d["snippet"] = (d["filename"] + " — " + d["title"])[:160]
            out.append(d)
        out.sort(key=lambda d: d["score"], reverse=True)
        return out

    # fallback: rango a similarità vettoriale TF-IDF (sola libreria
    # standard). I candidati arrivano da una ricerca FTS "soft" (OR tra
    # le parole presenti) o, per archivi piccoli, direttamente tutti i
    # documenti con testo; l'ordine è dato dal coseno query↔documento.
    vecs, idf = tfidf_vectors(conn, max_docs=50_000)
    qtokens = tokenize(free)
    if not qtokens:
        return []
    qvec = _normalize({t: idf.get(t, 1.0) for t in qtokens})

    if allowed is not None:
        candidates = [i for i in vecs if i in allowed]
    elif len(vecs) <= 5000:
        candidates = list(vecs.keys())
    else:
        present = [t for t in qtokens if t in idf]
        if not present:
            return []
        match = " OR ".join(f'"{t}"' for t in present)
        rows = conn.execute(
            "SELECT d.id FROM documents d JOIN docs_fts ON docs_fts.rowid=d.id "
            "WHERE docs_fts MATCH ? LIMIT 400", (match,)).fetchall()
        candidates = [r["id"] for r in rows]

    scored = []
    for i in candidates:
        v = vecs.get(i)
        if v:
            s = _cosine_sparse(qvec, _normalize(v))
            if s > 0.01:
                scored.append((s, i))
    scored.sort(reverse=True)
    top_ids = [i for _, i in scored[:limit]]
    scores = {i: s for s, i in scored[:limit]}
    out = []
    for r in conn.execute(
            "SELECT id, path, filename, title, kind, ext, size, date, author "
            "FROM documents WHERE id IN (%s)" % ",".join("?" * len(top_ids)),
            top_ids):
        d = dict(r)
        d["score"] = round(float(scores.get(r["id"], 0)), 4)
        d["snippet"] = (d["filename"] + " — " + d["title"])[:160]
        out.append(d)
    out.sort(key=lambda d: d["score"], reverse=True)
    return out


# ------------------------------------------------------------- clustering --

def cluster_documents(conn: sqlite3.Connection, k: int = 40, iters: int = 8,
                      progress: dict | None = None,
                      min_chars: int = 250,
                      kinds: tuple | None = None) -> dict:
    """k-means sui vettori (embedding o TF-IDF): assegna documents.topic.

    Ogni cluster riceve un'etichetta con i termini più distintivi. Vengono
    raggruppati solo i documenti con testo reale (oltre `min_chars`
    caratteri) e, di default, solo i tipi «documento» (PDF, Office,
    email...): i file indicizzati col solo nome produrrebbero gruppi
    senza senso («dll», «jar», «license»...). Passa kinds=None per
    includere tutto.
    """
    stats = progress if progress is not None else {}
    stats.update({"fase": "vettori", "done": 0, "total": 0, "fatto": False,
                  "errore": None, "cluster": 0})
    try:
        # solo documenti con contenuto testivo effettivo
        kinds = tuple(kinds) if kinds is not None else DOC_KINDS
        ph = ",".join("?" * len(kinds))
        doc_ids_f = {r["id"] for r in conn.execute(
            f"SELECT id FROM documents WHERE LENGTH(text) > ? "
            f"AND kind IN ({ph})", [min_chars] + list(kinds))}
        data = _load_embeddings_np(conn)
        if data is not None:
            # k-means vettorizzato (numpy) sulla media dei blocchi di
            # ogni documento: veloce anche su decine di migliaia di file
            import numpy as np
            mask = np.isin(data["doc_ids"], np.array(sorted(doc_ids_f),
                                                     dtype=np.int64))
            e_ids = data["doc_ids"][mask]
            # matrice a livello documento: media dei blocchi, rinormalizzata
            sums = np.add.reduceat(data["mat"], data["starts"], axis=0)
            counts = np.diff(np.append(data["starts"],
                                       len(data["row_doc"]))).astype(
                np.float32).reshape(-1, 1)
            doc_mat = sums / np.maximum(counts, 1.0)
            norms = np.linalg.norm(doc_mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            e_mat = (doc_mat / norms)[mask]
            n = len(e_ids)
            k = max(2, min(k, n))
            rng = np.random.default_rng(42)
            centroids = e_mat[rng.choice(n, size=k, replace=False)].copy()
            assign = np.zeros(n, dtype=np.int64)
            for it in range(iters):
                stats["fase"] = f"iterazione {it + 1}/{iters}"
                sims = e_mat @ centroids.T          # n × k
                new_assign = np.argmax(sims, axis=1)
                changed = int(np.count_nonzero(new_assign != assign))
                assign = new_assign
                stats["done"] = (it + 1) * n // iters
                for ci in range(k):
                    mask = assign == ci
                    if mask.any():
                        centroids[ci] = e_mat[mask].mean(axis=0)
                        norms = np.linalg.norm(centroids[ci])
                        if norms:
                            centroids[ci] /= norms
                if not changed:
                    break
            group_of = {int(i): int(a) for i, a in zip(e_ids, assign)}
        else:
            # TF-IDF sparsi in puro Python
            vecs, idf = tfidf_vectors(conn)
            vecs = {i: v for i, v in vecs.items() if i in doc_ids_f}
            ids = list(vecs.keys())
            n = len(ids)
            k = max(2, min(k, n))
            rng = random.Random(42)
            centroids = [_normalize(dict(vecs[i])) for i in rng.sample(ids, k)]
            assign = {}
            for it in range(iters):
                stats["fase"] = f"iterazione {it + 1}/{iters}"
                changed = 0
                for i in ids:
                    v = vecs[i]
                    best, bi = -1.0, 0
                    for ci, c in enumerate(centroids):
                        s = _cosine_sparse(v, c)
                        if s > best:
                            best, bi = s, ci
                    if assign.get(i) != bi:
                        assign[i] = bi
                        changed += 1
                stats["done"] = (it + 1) * n // iters
                for ci in range(k):
                    mem = [vecs[i] for i in ids if assign.get(i) == ci]
                    if mem:
                        acc: dict[str, float] = defaultdict(float)
                        for m in mem:
                            for t, w in m.items():
                                acc[t] += w
                        centroids[ci] = _normalize(dict(acc))
                if not changed:
                    break
            group_of = assign

        stats["fase"] = "etichette"
        # etichette: termini più distintivi di ogni cluster (TF-IDF medio)
        vecs_t, idf_t = tfidf_vectors(conn)
        sums_by_cluster: dict[int, dict[str, float]] = defaultdict(
            lambda: defaultdict(float))
        for i, g in group_of.items():
            if i in vecs_t:
                for t, w in vecs_t[i].items():
                    if w > 0:
                        sums_by_cluster[g][t] += w
        sizes = Counter(group_of.values())
        labels: dict[int, str] = {}
        for g, terms in sums_by_cluster.items():
            top = sorted(terms.items(), key=lambda kv: -kv[1])[:4]
            words = [t for t, _ in top][:3]
            labels[g] = " · ".join(words) if words else "argomento"
        conn.execute("UPDATE documents SET topic = ''")
        by_group: dict[int, list[int]] = defaultdict(list)
        for i, g in group_of.items():
            by_group[g].append(i)
        for g, members in sorted(by_group.items(),
                                 key=lambda kv: -len(kv[1])):
            label = f"{g + 1:02d} · {labels.get(g, 'argomento')}"
            conn.execute(
                "UPDATE documents SET topic = ? WHERE id IN (%s)"
                % ",".join("?" * len(members)),
                [label] + members)
        conn.commit()
        stats["cluster"] = len(by_group)
        stats["fatto"] = True
        return stats
    except Exception as exc:
        stats["errore"] = str(exc)
        stats["fatto"] = True
        return stats


# ------------------------------------------------- suggerimento gruppi ----

def similar_semantic(conn: sqlite3.Connection, doc_id: int,
                     limit: int = 8) -> tuple[bool, list] | None:
    """Documenti più affini per significato a quello dato.

    Con i blocchi: affinità(A,B) = massimo del coseno tra ogni blocco di
    A e ogni blocco di B (basta una parte in comune). Ritorna
    (usati_embedding, elementi) oppure None se il documento non ha
    embedding (chiamante: fallback TF-IDF).
    """
    data = _load_embeddings_np(conn)
    if data is None:
        return None
    import numpy as np
    doc_pos = np.searchsorted(data["doc_ids"], doc_id)
    if doc_pos >= len(data["doc_ids"]) or data["doc_ids"][doc_pos] != doc_id:
        return None
    s = int(data["starts"][doc_pos])
    e = int(data["starts"][doc_pos + 1]) if doc_pos + 1 < len(data["starts"]) \
        else len(data["row_doc"])
    Q = data["mat"][s:e]                      # blocchi del documento
    row_max = (data["mat"] @ Q.T).max(axis=1)  # miglior blocco per riga
    doc_sims = np.maximum.reduceat(row_max, data["starts"])
    doc_sims[~data["doc_real"]] = -1.0
    doc_sims[doc_pos] = -1.0                  # esclude il documento stesso
    order = np.argsort(-doc_sims)[:limit]
    top_ids = [int(data["doc_ids"][j]) for j in order]
    scores = {int(data["doc_ids"][j]): float(doc_sims[j]) for j in order}
    items = []
    for r in conn.execute(
            "SELECT id, title, kind, filename FROM documents "
            "WHERE id IN (%s)" % ",".join("?" * len(top_ids)), top_ids):
        items.append({"id": r["id"], "title": r["title"],
                      "kind": r["kind"], "filename": r["filename"],
                      "score": round(scores.get(r["id"], 0.0), 4)})
    items.sort(key=lambda d: -d["score"])
    return True, items


# ------------------------------------------------------- concetti --------

def _parse_exts(raw: str) -> set[str]:
    out = set()
    for tok in re.split(r"[;, ]+", raw or ""):
        tok = tok.strip().lower().lstrip(".")
        if tok:
            out.add("." + tok)
    return out


def _parse_docs(conn, raw: str) -> list[int]:
    """Identifica i documenti campione: preferibilmente per PERCORSO
    assoluto sul filesystem (anche con spazi, uno per riga), ma sono
    accettati ancora gli ID numerici. I percorsi non trovati vengono
    ignorati."""
    out, seen = [], set()

    def add(did):
        if did and did not in seen:
            seen.add(did)
            out.append(did)

    def resolve(path: str):
        path = path.strip().strip('"')
        if not path:
            return
        variants = [path, path.replace("/", "\\"), path.replace("\\", "/")]
        for cand in variants:
            row = conn.execute(
                "SELECT id FROM documents WHERE path = ? COLLATE NOCASE",
                (cand,)).fetchone()
            if row:
                add(row["id"])
                return
        for cand in variants:
            row = conn.execute(
                "SELECT id FROM documents WHERE path LIKE ? COLLATE NOCASE "
                "ORDER BY LENGTH(path) LIMIT 1",
                ("%" + cand,)).fetchone()
            if row:
                add(row["id"])
                return

    for seg in re.split(r"[;\n]+", raw or ""):
        seg = seg.strip()
        if not seg:
            continue
        if seg.isdigit():
            add(int(seg))
        elif (":" in seg[:3]) or "/" in seg or "\\" in seg or \
                re.search(r"\.\w{1,6}$", seg):
            resolve(seg)
        else:
            for tok in seg.split():
                if tok.isdigit():
                    add(int(tok))
    return out


def _parse_phrases(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;\n]+", raw or "") if p.strip()]


def _doc_matrix(data):
    """Matrice a livello documento (media dei blocchi, normalizzata),
    calcolata una volta e memorizzata nella cache dei dati."""
    if "doc_mat" in data:
        return data["doc_mat"]
    import numpy as np
    sums = np.add.reduceat(data["mat"], data["starts"], axis=0)
    counts = np.diff(np.append(data["starts"],
                               len(data["row_doc"]))).astype(
        np.float32).reshape(-1, 1)
    doc_mat = sums / np.maximum(counts, 1.0)
    norms = np.linalg.norm(doc_mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    doc_mat = doc_mat / norms
    data["doc_mat"] = doc_mat
    return doc_mat


def _split_refs(phrases: list[str]) -> tuple[list[str], list[str]]:
    """Separa i richiami «@Nome» (altri concetti o gruppi sem:) dalle
    frasi normali negli ancoraggi."""
    plain, refs = [], []
    for p in phrases:
        if p.startswith("@") and len(p) > 1:
            refs.append(p[1:].strip())
        else:
            plain.append(p)
    return plain, refs


def _resolve_ref(conn, data, name: str, depth: int, seen: frozenset):
    """Vettore-ancora di un concetto o di un gruppo sem: referenziato
    con @Nome. None se non risolvibile."""
    row = conn.execute(
        "SELECT * FROM concepts WHERE name = ? COLLATE NOCASE",
        (name,)).fetchone()
    if row:
        ph, refs = _split_refs(_parse_phrases(row["pos_text"]))
        return _anchor_vector(conn, data, ph, _parse_docs(conn, row["pos_docs"]),
                              refs, depth + 1, seen)
    g = conn.execute(
        "SELECT query FROM groups WHERE name = ? COLLATE NOCASE",
        (name,)).fetchone()
    if g and g["query"]:
        if not embeddings_status()[0]:
            return None
        import numpy as np
        q = g["query"][4:] if g["query"].startswith("sem:") else g["query"]
        v = np.array(embed_query(q[:400]), dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else None
    return None


def _ref_words(conn, refs: list[str], depth: int = 0,
               seen: frozenset = frozenset()) -> list[str]:
    """Parole lessicali dei concetti/gruppi referenziati (per l'unione
    lessicale dei loro positivi)."""
    out: list[str] = []
    for name in refs[:6]:
        key = name.lower()
        if key in seen or depth >= 3:
            continue
        row = conn.execute(
            "SELECT pos_text FROM concepts WHERE name = ? COLLATE NOCASE",
            (name,)).fetchone()
        if row:
            ph, sub = _split_refs(_parse_phrases(row["pos_text"]))
            out.extend(ph)
            out.extend(_ref_words(conn, sub, depth + 1, seen | {key}))
            continue
        g = conn.execute(
            "SELECT query FROM groups WHERE name = ? COLLATE NOCASE",
            (name,)).fetchone()
        if g and g["query"]:
            q = g["query"][4:] if g["query"].startswith("sem:") else g["query"]
            out.append(q)
    return out


def _anchor_vector(conn, data, phrases: list[str], doc_ids: list[int],
                   refs: list[str] | None = None, depth: int = 0,
                   seen: frozenset = frozenset()):
    """Vettore ancoraggio: media normalizzata degli embedding delle
    frasi, dei vettori (a livello documento) dei campioni e degli
    ancore dei concetti/gruppi richiamati con @Nome. None se non c'è
    nessun contributo."""
    import numpy as np
    parts = []
    if phrases and embeddings_status()[0]:
        for p in phrases[:12]:
            try:
                v = np.array(embed_query(p), dtype=np.float32)
                n = float(np.linalg.norm(v))
                if n:
                    parts.append(v / n)
            except Exception:
                pass
    if doc_ids and data is not None:
        doc_mat = _doc_matrix(data)
        for did in doc_ids[:50]:
            pos = int(np.searchsorted(data["doc_ids"], did))
            if pos < len(data["doc_ids"]) and data["doc_ids"][pos] == did:
                parts.append(doc_mat[pos])
    for name in (refs or [])[:6]:
        key = name.lower()
        if key in seen or depth >= 3:
            continue
        sub = _resolve_ref(conn, data, name, depth + 1, seen | {key})
        if sub is not None:
            parts.append(sub)
    if not parts:
        return None
    anchor = np.mean(np.stack(parts), axis=0)
    n = float(np.linalg.norm(anchor))
    return anchor / n if n else None


def _lexical_ids(conn, phrases: list[str], limit: int = 20_000) -> set[int]:
    """Documenti che contengono almeno una delle parole delle frasi."""
    ids: set[int] = set()
    words = sorted({w.lower().strip("\"'()") for p in phrases
                    for w in p.split() if len(w) > 2})[:12]
    for w in words:
        try:
            ids |= {r["id"] for r in conn.execute(
                "SELECT d.id FROM documents d JOIN docs_fts f "
                "ON f.rowid=d.id WHERE docs_fts MATCH ? LIMIT ?",
                ('"%s"' % w.replace('"', '""'), limit))}
        except Exception:
            pass
    return ids


def concept_classify(conn: sqlite3.Connection, concept, limit: int = 500):
    """Classifica i documenti per un concetto: punteggio =
    media(sim(XX, A-parole), sim(XX, A-documenti)) − sim(XX, B),
    con i contributi lessicali e di tipologia file. Con ext_mode «solo»
    le tipologie positive diventano un filtro rigoroso. Negli ancoraggi
    «@Nome» richiama un altro concetto o una categoria sem:
    (combinazione di categorie), con protezione dai riferimenti
    circolari. Ritorna (n_classificati, elenco ordinato)."""
    import numpy as np
    data = _load_embeddings_np(conn)
    use_sem = data is not None and embeddings_status()[0]

    own = (concept["name"] or "").lower()
    pos_phr, pos_refs = _split_refs(_parse_phrases(concept["pos_text"]))
    neg_phr, neg_refs = _split_refs(_parse_phrases(concept["neg_text"]))
    seen0 = frozenset({own}) if own else frozenset()
    # A-parole: frasi e categorie richiamate; A-documenti: i campioni
    anchor_w = _anchor_vector(conn, data, pos_phr, [], pos_refs,
                              0, seen0) if use_sem else None
    pos_docs = _parse_docs(conn, concept["pos_docs"])
    anchor_d = _anchor_vector(conn, data, [], pos_docs, [],
                              0, seen0) if use_sem and pos_docs else None
    anchor_b = _anchor_vector(conn, data, neg_phr,
                              _parse_docs(conn, concept["neg_docs"]),
                              neg_refs, 0, seen0) if use_sem else None

    lex_pos = _lexical_ids(conn, pos_phr +
                           _ref_words(conn, pos_refs, 0, seen0))
    lex_neg = _lexical_ids(conn, neg_phr +
                           _ref_words(conn, neg_refs, 0, seen0))
    ext_pos = _parse_exts(concept["pos_exts"])
    ext_neg = _parse_exts(concept["neg_exts"])
    keys = concept.keys() if hasattr(concept, "keys") else []
    ext_mode = (concept["ext_mode"] if "ext_mode" in keys else None) or "bonus"
    strict = ext_mode == "solo" and bool(ext_pos)
    threshold = float(concept["threshold"])

    LEX_BONUS = 0.08      # una parola dell'ancoraggio è presente nel testo
    EXT_POS_BONUS = 0.05  # tipologia file positiva
    EXT_NEG_MALUS = 0.15  # tipologia file negativa: allontana deciso

    results = []  # (score, sim_parole, sim_documenti, sim_b, doc_id)
    if use_sem and any(a is not None for a in (anchor_w, anchor_d, anchor_b)):
        doc_mat = _doc_matrix(data)
        n_docs = len(doc_mat)
        sim_w = doc_mat @ anchor_w if anchor_w is not None else None
        sim_d = doc_mat @ anchor_d if anchor_d is not None else None
        sim_b = doc_mat @ anchor_b if anchor_b is not None \
            else np.zeros(n_docs)
        docs = conn.execute(
            "SELECT id, ext FROM documents WHERE text != ''").fetchall()
        ext_by_id = {r["id"]: (r["ext"] or "").lower() for r in docs}
        for j, did in enumerate(data["doc_ids"]):
            did = int(did)
            ext = ext_by_id.get(did, "")
            if strict and ext not in ext_pos:
                continue
            parts = [float(v[j]) for v in (sim_w, sim_d) if v is not None]
            a = sum(parts) / len(parts) if parts else 0.0
            score = a - float(sim_b[j])
            if did in lex_pos:
                score += LEX_BONUS
            if did in lex_neg:
                score -= LEX_BONUS
            if ext and ext in ext_pos:
                score += EXT_POS_BONUS
            if ext and ext in ext_neg:
                score -= EXT_NEG_MALUS
            if score >= threshold:
                results.append((score,
                                float(sim_w[j]) if sim_w is not None else 0.0,
                                float(sim_d[j]) if sim_d is not None else 0.0,
                                float(sim_b[j]), did))
    else:
        # senza embedding: solo contributi lessicali e di tipologia
        for did in lex_pos:
            ext = conn.execute("SELECT ext FROM documents WHERE id=?",
                               (did,)).fetchone()
            ext = (ext[0] or "").lower() if ext else ""
            if strict and ext not in ext_pos:
                continue
            score = LEX_BONUS
            if ext in ext_pos:
                score += EXT_POS_BONUS
            if ext in ext_neg:
                score -= EXT_NEG_MALUS
            if did in lex_neg:
                score -= LEX_BONUS
            if score >= threshold:
                results.append((score, 0.0, 0.0, 0.0, did))

    results.sort(reverse=True)
    top = results[:limit]
    out = []
    if top:
        idl = [x[-1] for x in top]
        for r in conn.execute(
                "SELECT id, path, filename, title, kind, ext, size, date, "
                "author FROM documents WHERE id IN (%s)"
                % ",".join("?" * len(idl)), idl):
            s, w, dd, b = next((x[:4] for x in top if x[4] == r["id"]),
                               (0, 0, 0, 0))
            out.append(dict(r, score=round(s, 4), sim_w=round(w, 4),
                            sim_d=round(dd, 4), sim_b=round(b, 4)))
    out.sort(key=lambda d: -d["score"])
    # cache del conteggio per l'elenco categorie (senza riclassificare)
    try:
        conn.execute("UPDATE concepts SET members=? WHERE id=?",
                     (len(results), concept["id"]))
        conn.commit()
    except Exception:
        pass
    return len(results), out


def suggest_groups_semantic(conn: sqlite3.Connection, doc_id: int,
                            limit: int = 4) -> list[dict] | None:
    """Gruppi manuali consigliati per contenuto, sui vettori embedding.

    Ritorna None se il documento non ha embedding (o non ci sono
    embedding affatto): il chiamante decide il fallback.
    """
    data = _load_embeddings_np(conn)
    if data is None:
        return None
    import numpy as np
    if "doc_mat" not in data:
        sums = np.add.reduceat(data["mat"], data["starts"], axis=0)
        counts = np.diff(np.append(data["starts"],
                                   len(data["row_doc"]))).astype(
            np.float32).reshape(-1, 1)
        doc_mat = sums / np.maximum(counts, 1.0)
        norms = np.linalg.norm(doc_mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        data["doc_mat"] = doc_mat / norms
    doc_pos = int(np.searchsorted(data["doc_ids"], doc_id))
    if doc_pos >= len(data["doc_ids"]) or data["doc_ids"][doc_pos] != doc_id:
        return None
    target = data["doc_mat"][doc_pos]
    out = []
    for g in conn.execute(
            "SELECT g.id, g.name, (SELECT COUNT(*) FROM doc_groups dg "
            "WHERE dg.group_id = g.id) AS n FROM groups g "
            "WHERE g.query IS NULL"):
        members = [r["doc_id"] for r in conn.execute(
            "SELECT doc_id FROM doc_groups WHERE group_id=?", (g["id"],))]
        idx = np.flatnonzero(np.isin(data["doc_ids"],
                                     np.array(members, dtype=np.int64)))
        if len(idx) == 0:
            continue
        centroid = data["doc_mat"][idx].mean(axis=0)
        norm = float(np.linalg.norm(centroid)) or 1.0
        score = float(target @ (centroid / norm))
        if score > 0.05:
            out.append({"id": g["id"], "name": g["name"], "n": len(members),
                        "score": round(score, 4)})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]


def suggest_groups(conn: sqlite3.Connection, doc_id: int,
                   limit: int = 4) -> list[dict]:
    """Gruppi manuali consigliati per un documento (centroide + coseno)."""
    groups = conn.execute(
        "SELECT g.id, g.name, (SELECT COUNT(*) FROM doc_groups dg "
        "WHERE dg.group_id = g.id) AS n FROM groups g "
        "WHERE g.query IS NULL").fetchall()
    if not groups:
        return []
    vecs, _ = tfidf_vectors(conn, max_docs=50_000)
    if doc_id not in vecs:
        return []
    target = _normalize(vecs[doc_id])
    out = []
    for g in groups:
        members = [r["doc_id"] for r in conn.execute(
            "SELECT doc_id FROM doc_groups WHERE group_id=?", (g["id"],))]
        member_vecs = [_normalize(vecs[m]) for m in members if m in vecs]
        if not member_vecs:
            continue
        centroid = _normalize({
            t: sum(v.get(t, 0.0) for v in member_vecs) / len(member_vecs)
            for v in member_vecs for t in v})
        score = _cosine_sparse(target, centroid)
        if score > 0.02:
            out.append({"id": g["id"], "name": g["name"], "n": len(members),
                        "score": round(float(score), 4)})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]
