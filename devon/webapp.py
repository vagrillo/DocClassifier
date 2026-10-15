"""Interfaccia grafica web di devon (sola libreria standard).

Avvio:  python3 -m devon serve            (apre anche il browser)
        python3 -m devon serve --port 8300 --no-browser

Architettura: il server espone una piccola API JSON sotto /api/ e la
shell dell'applicazione (HTML+CSS+JS vanilla, senza dipendenze) che la
interroga via fetch. Le azioni (tag, gruppi, import, rimozione
dall'indice) avvengono direttamente dall'interfaccia; l'import di una
cartella gira in un thread dedicato con avanzamento in tempo reale.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import db as dbm
from . import ingest as ingestm
from . import search as searchm
from . import semantic as semanticm
from . import similar as similarm

# --- stato dell'import in background (una sola alla volta) ---------------

_import_lock = threading.Lock()
_import_state: dict = {"running": False, "path": None, "stats": None,
                       "error": None}

# --- stato dei lavori semantici (embedding / clustering) ------------------

_jobs: dict[str, dict] = {
    "embed": {"running": False, "stats": None},
    "cluster": {"running": False, "stats": None},
}


def _embed_then_cluster(conn, progress):
    """Embedding di tutti i documenti e, se riesce, raggruppamento."""
    stats = semanticm.embed_all(conn, progress)
    if stats.get("errore") or not stats.get("done"):
        return
    jstats = {"fase": "embedding completato", "done": 0, "total": 0,
              "fatto": False, "errore": None, "cluster": 0}
    _jobs["cluster"].update(running=True, stats=jstats)
    try:
        semanticm.cluster_documents(conn, k=40, progress=jstats)
    except Exception as exc:
        jstats["errore"] = str(exc)
    finally:
        jstats["fatto"] = True
        _jobs["cluster"]["running"] = False


def start_job(kind: str, db_path, fn, *args, **kwargs) -> str | None:
    """Avvia un lavoro semantico in background (uno per tipo)."""
    state = _jobs[kind]
    if state["running"]:
        return {"embed": "Calcolo embedding già in corso.",
                "cluster": "Raggruppamento già in corso."}[kind]
    state.update(running=True, stats={"fatto": False})
    state["stats"] = stats = {
        "embed": {"done": 0, "total": 0, "fatto": False, "errore": None},
        "cluster": {"fase": "avvio", "done": 0, "total": 0, "fatto": False,
                    "errore": None, "cluster": 0},
    }[kind]

    def worker():
        try:
            fn(dbm.connect(db_path), *args, progress=stats, **kwargs)
        except Exception as exc:
            stats["errore"] = str(exc)
        finally:
            state["running"] = False
            stats["fatto"] = True

    threading.Thread(target=worker, daemon=True).start()
    return None


def _write_retry(fn, attempts: int = 6, wait: float = 2.0):
    """Esegue una scrittura sul database ritentando se è bloccata da un
    altro lavoro (import in corso). Solleva l'ultimo errore dopo i
    tentativi."""
    last = None
    for i in range(attempts):
        try:
            fn()
            return True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            last = exc
            time.sleep(wait)
    raise last


def start_import(db_path, raw_paths, source_ids=None) -> str | None:
    """Avvia l'indicizzazione di una o più radici in background.

    `raw_paths` è una stringa o una lista di percorsi (indicizzati in
    sequenza). Se vengono passati gli `source_ids`, a fine lavoro la
    tabella sources viene aggiornata con esito e data della scansione.
    Ritorna None se tutto bene, altrimenti un messaggio d'errore."""
    with _import_lock:
        if _import_state["running"]:
            return "Un'importazione è già in corso."
        if isinstance(raw_paths, str):
            paths = [raw_paths]
        else:
            paths = list(raw_paths)
        if not paths:
            return "Nessun percorso fornito."
        resolved = []
        for raw in paths:
            p = Path(str(raw)).expanduser()
            if not p.exists():
                return f"Percorso inesistente: {p}"
            resolved.append(p)
        _import_state.update(
            running=True, path=" + ".join(str(p) for p in resolved),
            error=None,
            stats={"visti": 0, "nuovi": 0, "aggiornati": 0, "doppioni": 0,
                   "saltati": 0, "errori": 0, "fatto": False, "ultimo": "",
                   "radice": ""})

    def worker():
        try:
            conn = dbm.connect(db_path)
            stats = ingestm.ingest_folder(
                conn, roots=resolved, verbose=False,
                progress=_import_state["stats"])
            if source_ids:
                import datetime as _dt
                import json as _json
                conn.executemany(
                    "UPDATE sources SET last_scan = ?, last_stats = ? "
                    "WHERE id = ?",
                    [(_dt.datetime.now().isoformat(timespec="seconds"),
                      _json.dumps(stats, ensure_ascii=False, default=str),
                      sid) for sid in source_ids])
                conn.commit()
        except Exception as exc:
            _import_state["error"] = str(exc)
        finally:
            _import_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return None


# --- tipi di file serviti da /file/N --------------------------------------

INLINE_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".bmp": "image/bmp",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8", ".json": "application/json",
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".xml": "text/xml; charset=utf-8", ".eml": "message/rfc822",
}


# --- handler ---------------------------------------------------------------

def smart_group_members(conn, query: str, limit: int = 2000):
    """Membri di un gruppo smart. Con prefisso «sem:» unisce le
    corrispondenze lessicali (FTS, in OR tra le parole della query) e
    quelle semantiche sopra soglia (0.5: conservativa)."""
    if not query.startswith("sem:"):
        return [dict(r) for r in searchm.search(conn, query, limit=limit)]
    term = query[4:].strip()
    if not term:
        return []
    ids = set()
    words = [t for t in term.split() if len(t) > 2]
    if words:
        match = " OR ".join('"%s"' % w.replace('"', '""') for w in words)
        ids |= {r["id"] for r in conn.execute(
            "SELECT d.id FROM documents d JOIN docs_fts ON docs_fts.rowid=d.id "
            "WHERE docs_fts MATCH ? LIMIT ?", (match, limit))}
    try:
        ids |= {r["id"] for r in semanticm.semantic_search(conn, term,
                                                           limit=limit)
                if r.get("score", 0) >= 0.5}
    except Exception:
        pass
    if not ids:
        return []
    idl = sorted(ids)
    rows = conn.execute(
        "SELECT id, path, filename, title, kind, ext, size, date, author "
        "FROM documents WHERE id IN (%s) ORDER BY date DESC, id DESC "
        "LIMIT ?" % ",".join("?" * len(idl)), idl + [limit]).fetchall()
    return [dict(r) for r in rows]


# --- catalogo di categorie di vita pronte all'uso -------------------------

PRESET_CATALOG: dict[str, str] = {
    "famiglia": "mamma\nmadre\npapà\npadre\nfratello\nsorella\nfiglio\n"
                "figlia\nfamiglia\ngenitori\nnonno\nnonna\nmatrimonio\nfigli",
    "salute": "salute\nmedico\ndottore\nospedale\nricetta\nreferto\nanalisi\n"
              "visita\nfarmacia\ncura\ndiagnosi\ntessera sanitaria\nclinica",
    "lavoro": "lavoro\nufficio\nazienda\nbusta paga\ncontratto\ncollega\n"
              "riunione\ndatore\ndipendente\nferie\nsmart working\nCED",
    "finanza": "banca\nconto corrente\nmutuo\nprestito\ntasse\nfisco\n730\n"
               "dichiarazione\nimposte\nfattura\ninvestimento\nspese",
    "casa": "casa\naffitto\ncondominio\nbollette\nutenze\nproprietario\n"
            "locazione\nappartamento\nristrutturazione\ncaldaia",
    "veicoli": "auto\nautomobile\nmacchina\npatente\nbollo\nrevisione\nmulta\n"
               "veicolo\ngaranzia\nofficina",
    "viaggi": "viaggio\nvacanza\nbiglietto\nprenotazione\nhotel\nvolo\n"
              "passaporto\nitinerario\nvaligia\naeroporto",
    "identita": "carta d'identità\ncodice fiscale\ntessera sanitaria\n"
                "passaporto\ncertificato\nresidenza\nanagrafe\nprocura",
    "scuola": "scuola\nuniversità\nesame\nlaurea\nvoto\ninsegnante\ncompiti\n"
              "iscrizione\npagella\nlibro di testo",
    "hobby": "hobby\nsport\nricetta\ncucina\nfotografia\nmusica\n"
             "collezione\npartita",
}


def _preset_rows(conn) -> list[dict]:
    existing = {r["name"].lower() for r in
                conn.execute("SELECT name FROM keywords")}
    return [{"name": n, "terms": t, "exists": n.lower() in existing}
            for n, t in PRESET_CATALOG.items()]


def _write_keyword(conn, name: str, terms: str) -> None:
    import datetime as _dt
    conn.execute(
        "INSERT INTO keywords(name, terms, created) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET terms=excluded.terms",
        (name, terms, _dt.datetime.now().isoformat(timespec="seconds")))


class Handler(BaseHTTPRequestHandler):
    db_path: object = None

    def log_message(self, fmt, *args):
        pass  # log silenzioso

    # --- primitive di risposta --------------------------------------------

    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, data) -> None:
        self._json({"ok": True, "data": data})

    def _err(self, msg: str, status: int = 400) -> None:
        self._json({"ok": False, "error": msg}, status)

    def _html(self, html_str: str) -> None:
        data = html_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, download: bool) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._err(f"File non leggibile: {exc}", 404)
            return
        ctype = INLINE_TYPES.get(path.suffix.lower()) or \
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        disp = "attachment" if download else "inline"
        quoted = path.name.replace('"', "'")
        self.send_header("Content-Disposition",
                         f'{disp}; filename="{quoted}"')
        self.end_headers()
        self.wfile.write(data)

    # --- instradamento ------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        qs = parse_qs(url.query)
        try:
            if not parts:
                self._html(GAPP_SHELL)
            elif parts == ["classic"]:
                self._html(APP_SHELL)
            elif parts == ["favicon.ico"]:
                self.send_response(204)
                self.end_headers()
            elif parts[0] == "file" and len(parts) == 2 and parts[1].isdigit():
                self._route_file(int(parts[1]),
                                 download=qs.get("download", [""])[0] == "1")
            elif parts[0] == "api":
                self._route_api_get(parts[1:], qs)
            else:
                self._err("Risorsa inesistente", 404)
        except Exception as exc:
            self._err(f"Errore interno: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if parts[:2] == ["api", "import"]:
                payload = body.get("paths") or body.get("path") or ""
                err = start_import(self.db_path, payload)
                self._err(err, 409) if err else self._ok("avviato")
            elif parts[:2] == ["api", "sources"]:
                action = parts[2] if len(parts) > 2 else "add"
                if action == "add":
                    import datetime as _dt
                    path = str(body.get("path", "")).strip()
                    if not path:
                        self._err("Percorso vuoto")
                        return
                    p = Path(path).expanduser()
                    if not p.exists():
                        self._err(f"Percorso inesistente: {p}", 404)
                        return
                    conn = dbm.connect(self.db_path)
                    conn.execute(
                        "INSERT OR IGNORE INTO sources(path, added) "
                        "VALUES(?,?)",
                        (str(p), _dt.datetime.now().isoformat(
                            timespec="seconds")))
                    conn.commit()
                    self._ok(str(p))
                elif action == "remove":
                    conn = dbm.connect(self.db_path)
                    cur = conn.execute("DELETE FROM sources WHERE id=?",
                                       (body.get("id"),))
                    conn.commit()
                    self._ok("rimossa" if cur.rowcount else "inesistente")
                elif action == "scan":
                    conn = dbm.connect(self.db_path)
                    if body.get("id") is not None:
                        rows = conn.execute(
                            "SELECT id, path FROM sources WHERE id=?",
                            (body["id"],)).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT id, path FROM sources ORDER BY id"
                        ).fetchall()
                    if not rows:
                        self._err("Nessuna cartella da indicizzare")
                        return
                    err = start_import(
                        self.db_path, [r["path"] for r in rows],
                        source_ids=[r["id"] for r in rows])
                    self._err(err, 409) if err else self._ok("avviato")
                else:
                    self._err("Azione inesistente", 404)
            elif parts[:2] == ["api", "keywords"]:
                action = parts[2] if len(parts) > 2 else "save"
                if action == "save":
                    import datetime as _dt
                    name = str(body.get("name", "")).strip().lstrip("#")
                    terms = str(body.get("terms", "") or "").strip()
                    if not name or not terms:
                        self._err("Servono nome e termini (uno per riga)")
                        return
                    conn = dbm.connect(self.db_path)

                    def do_write():
                        conn.execute(
                            "INSERT INTO keywords(name, terms, created) "
                            "VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                            "terms=excluded.terms",
                            (name, terms, _dt.datetime.now().isoformat(
                                timespec="seconds")))

                    _write_retry(do_write)
                    conn.commit()
                    self._ok(name)
                elif action == "remove":
                    conn = dbm.connect(self.db_path)

                    def do_rm():
                        conn.execute("DELETE FROM keywords WHERE name=? "
                                     "COLLATE NOCASE",
                                     (str(body.get("name", "")).lstrip("#"),))

                    _write_retry(do_rm)
                    conn.commit()
                    self._ok("rimossa")
                else:
                    self._err("Azione inesistente", 404)
            elif parts[:2] == ["api", "concepts"]:
                action = parts[2] if len(parts) > 2 else "save"
                if action == "save":
                    import datetime as _dt
                    name = str(body.get("name", "")).strip()
                    if not name:
                        self._err("Nome del concetto obbligatorio")
                        return
                    fields = {k: str(body.get(k, "") or "").strip()
                              for k in ("pos_text", "pos_docs", "pos_exts",
                                        "neg_text", "neg_docs", "neg_exts")}
                    try:
                        threshold = float(body.get("threshold", 0.10))
                    except (TypeError, ValueError):
                        threshold = 0.10
                    ext_mode = "solo" if body.get("ext_mode") == "solo" \
                        else "bonus"
                    conn = dbm.connect(self.db_path)

                    def do_write():
                        if body.get("id"):
                            conn.execute(
                                "UPDATE concepts SET name=?, pos_text=?, "
                                "pos_docs=?, pos_exts=?, neg_text=?, "
                                "neg_docs=?, neg_exts=?, threshold=?, "
                                "ext_mode=? WHERE id=?",
                                (name,) + tuple(fields.values()) +
                                (threshold, ext_mode, body["id"]))
                            return body["id"]
                        existing = conn.execute(
                            "SELECT id FROM concepts WHERE name=? "
                            "COLLATE NOCASE", (name,)).fetchone()
                        if existing:
                            # stesso nome: aggiorna invece di duplicare
                            conn.execute(
                                "UPDATE concepts SET pos_text=?, pos_docs=?, "
                                "pos_exts=?, neg_text=?, neg_docs=?, "
                                "neg_exts=?, threshold=?, ext_mode=? "
                                "WHERE id=?",
                                tuple(fields.values()) +
                                (threshold, ext_mode, existing["id"]))
                            return existing["id"]
                        cur = conn.execute(
                            "INSERT INTO concepts(name, pos_text, "
                            "pos_docs, pos_exts, neg_text, neg_docs, "
                            "neg_exts, threshold, ext_mode, created) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (name,) + tuple(fields.values()) +
                            (threshold, ext_mode,
                             _dt.datetime.now().isoformat(
                                 timespec="seconds")))
                        return cur.lastrowid

                    cid = _write_retry(do_write)
                    conn.commit()
                    self._ok(cid)
                elif action == "remove":
                    conn = dbm.connect(self.db_path)
                    conn.execute("DELETE FROM concepts WHERE id=?",
                                 (body.get("id"),))
                    conn.commit()
                    self._ok("rimosso")
                elif action == "add-doc":
                    conn = dbm.connect(self.db_path)
                    col = "pos_docs" if body.get("polarity") == "pos" \
                        else "neg_docs"
                    drow = conn.execute(
                        "SELECT path FROM documents WHERE id=?",
                        (body.get("doc_id"),)).fetchone()
                    if not drow:
                        self._err("Documento inesistente", 404)
                        return
                    crow = conn.execute(
                        "SELECT %s AS v FROM concepts WHERE id=?" % col,
                        (body.get("id"),)).fetchone()
                    if not crow:
                        self._err("Concetto inesistente", 404)
                        return
                    items = [t.strip() for t in
                             re.split(r"[;\n]+", crow["v"] or "") if t.strip()]
                    if drow["path"] not in items:
                        items.append(drow["path"])
                    conn.execute("UPDATE concepts SET %s=? WHERE id=?" % col,
                                 ("\n".join(items), body["id"]))
                    conn.commit()
                    self._ok("aggiunto")
                else:
                    self._err("Azione inesistente", 404)
            elif parts[:2] == ["api", "presets"]:
                names = body.get("names") or []
                if isinstance(names, str):
                    names = [names]
                conn = dbm.connect(self.db_path)

                def do_write():
                    for n in names:
                        n = str(n).strip().lstrip("#").lower()
                        if n in PRESET_CATALOG:
                            _write_keyword(conn, n, PRESET_CATALOG[n])

                _write_retry(do_write)
                conn.commit()
                self._ok("create")
            elif parts[:2] == ["api", "embed"]:
                ok, msg = semanticm.embeddings_status()
                if not ok:
                    self._err(msg)
                else:
                    err = start_job("embed", self.db_path,
                                    _embed_then_cluster)
                    self._err(err, 409) if err else self._ok("avviato")
            elif parts[:2] == ["api", "cluster"]:
                k = max(2, min(int(body.get("k", 40)), 200))
                err = start_job("cluster", self.db_path,
                                semanticm.cluster_documents, k=k)
                self._err(err, 409) if err else self._ok("avviato")
            elif parts[:2] == ["api", "doc"] and len(parts) >= 3 \
                    and parts[2].isdigit():
                self._route_api_post_doc(int(parts[2]), parts[3:], body)
            else:
                self._err("Risorsa inesistente", 404)
        except Exception as exc:
            self._err(f"Errore interno: {exc}", 500)

    # --- API GET --------------------------------------------------------------

    def _route_file(self, doc_id: int, download: bool) -> None:
        conn = dbm.connect(self.db_path)
        d = conn.execute("SELECT path FROM documents WHERE id=?",
                         (doc_id,)).fetchone()
        if not d:
            self._err("Documento inesistente", 404)
            return
        p = Path(d["path"])
        if not p.exists():
            self._err("Il file non esiste più su disco", 410)
            return
        self._file(p, download)

    def _route_api_get(self, parts: list, qs: dict) -> None:
        conn = dbm.connect(self.db_path)

        if parts == ["status"]:
            docs = conn.execute(
                "SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            emb = conn.execute(
                "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
            self._ok({"docs": docs, "embeddings": emb,
                      "scanning": _import_state["running"]})
        elif parts == ["gsearch"]:
            q = qs.get("q", [""])[0].strip()
            limit = min(int(qs.get("limit", ["60"])[0]), 300)
            if not q:
                self._err("Query vuota")
                return
            results = semanticm.gsearch(conn, q, limit=limit)
            tagmap = self._tags_of(conn, [r["id"] for r in results])
            self._ok({"q": q, "n": len(results),
                      "results": [dict(r, tags=tagmap.get(r["id"], []))
                                  for r in results]})
        elif parts == ["keywords"]:
            self._ok([dict(r) for r in conn.execute(
                "SELECT name, terms FROM keywords ORDER BY name")])
        elif parts == ["presets"]:
            self._ok(_preset_rows(conn))
        elif parts == ["stats"]:
            self._ok(self._stats(conn))
        elif parts == ["semantic"]:
            q = qs.get("q", [""])[0].strip()
            limit = min(int(qs.get("limit", ["50"])[0]), 200)
            if not q:
                self._err("Query vuota")
                return
            results = semanticm.semantic_search(conn, q, limit=limit)
            tagmap = self._tags_of(conn, [r["id"] for r in results])
            self._ok([dict(r, tags=tagmap.get(r["id"], []))
                      for r in results])
        elif parts == ["topics"]:
            rows = conn.execute(
                "SELECT topic, COUNT(*) AS n FROM documents "
                "WHERE topic != '' GROUP BY topic ORDER BY n DESC "
                "LIMIT 100").fetchall()
            self._ok([dict(r) for r in rows])
        elif parts == ["sources"]:
            rows = conn.execute(
                "SELECT id, path, added, last_scan FROM sources "
                "ORDER BY id").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["exists"] = Path(r["path"]).exists()
                d["docs"] = conn.execute(
                    "SELECT COUNT(*) AS n FROM documents "
                    "WHERE path LIKE ?", (r["path"].rstrip("\\/") +
                                          "%",)).fetchone()["n"]
                out.append(d)
            self._ok(out)
        elif parts[:2] == ["concept", "list"] or parts == ["concepts"]:
            # elenco istantaneo: il conteggio è in cache (colonna members)
            # e viene aggiornato quando si apre la classificazione
            self._ok([dict(r) for r in conn.execute(
                "SELECT * FROM concepts ORDER BY name")])
        elif parts[:2] == ["concept", "get"] and len(parts) > 2 \
                and parts[2].isdigit():
            c = conn.execute("SELECT * FROM concepts WHERE id=?",
                             (int(parts[2]),)).fetchone()
            if not c:
                self._err("Concetto inesistente", 404)
                return
            total, results = semanticm.concept_classify(conn, c, limit=300)
            self._ok({"concept": dict(c), "total": total,
                      "results": results})
        elif parts[:2] == ["job", "embed"] or parts[:2] == ["job", "cluster"]:
            self._ok({"running": _jobs[parts[1]]["running"],
                      "stats": _jobs[parts[1]]["stats"],
                      "embeddings": conn.execute(
                          "SELECT COUNT(*) AS n FROM embeddings"
                      ).fetchone()["n"]})
        elif parts == ["search"]:
            q = qs.get("q", [""])[0]
            limit = min(int(qs.get("limit", ["200"])[0]), 1000)
            rows = searchm.search(conn, q, limit=limit) if q.strip() else \
                conn.execute(
                    "SELECT d.*, '' AS snippet, 0 AS rank FROM documents d "
                    "ORDER BY d.date DESC, d.id DESC LIMIT ?",
                    (limit,)).fetchall()
            ids = [r["id"] for r in rows]
            tagmap = self._tags_of(conn, ids)
            self._ok([dict(r, tags=tagmap.get(r["id"], [])) for r in rows])
        elif parts[0] == "doc" and len(parts) == 2 and parts[1].isdigit():
            self._api_doc(conn, int(parts[1]))
        elif parts == ["groups"]:
            rows = conn.execute(
                "SELECT id, name, parent_id, query, "
                "(SELECT COUNT(*) FROM doc_groups dg WHERE dg.group_id="
                "groups.id) AS n FROM groups ORDER BY name").fetchall()
            self._ok([dict(r) for r in rows])
        elif parts == ["dupes"]:
            rows = [dict(r) for r in conn.execute(
                "SELECT sha256, COUNT(*) AS n, MIN(title) AS title "
                "FROM documents GROUP BY sha256 "
                "HAVING n > 1 ORDER BY n DESC LIMIT 500")]
            for r in rows:
                doc_rows = conn.execute(
                    "SELECT id, path FROM documents WHERE sha256=? "
                    "ORDER BY path", (r["sha256"],)).fetchall()
                r["docs"] = [dict(x) for x in doc_rows]
            self._ok(rows)
        elif parts == ["import", "status"]:
            self._ok(dict(_import_state))
        else:
            self._err("Risorsa API inesistente", 404)

    def _api_doc(self, conn, doc_id: int) -> None:
        d = conn.execute("SELECT * FROM documents WHERE id=?",
                         (doc_id,)).fetchone()
        if not d:
            self._err("Documento inesistente", 404)
            return
        doc = dict(d)
        doc["tags"] = [r["tag"] for r in conn.execute(
            "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (doc_id,))]
        doc["groups"] = [dict(r) for r in conn.execute(
            "SELECT g.id, g.name FROM groups g JOIN doc_groups dg "
            "ON dg.group_id=g.id WHERE dg.doc_id=? ORDER BY g.name",
            (doc_id,))]
        smart = conn.execute(
            "SELECT name, query FROM groups WHERE query IS NOT NULL"
        ).fetchall()
        doc["smart_groups"] = [r["name"] for r in smart if doc_id in {
            x["id"] for x in smart_group_members(conn, r["query"])}]
        doc["exists"] = Path(doc["path"]).exists()
        have_embed = conn.execute(
            "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"] > 0
        sem = semanticm.similar_semantic(conn, doc_id)
        if sem is not None:
            doc["similar_semantic"] = True
            doc["similar"] = sem[1]
        elif not have_embed:
            # solo su macchine senza embedding: fallback TF-IDF (costoso,
            # ritokenizza il corpus: inaccettabile a ogni clic altrove)
            doc["similar_semantic"] = False
            doc["similar"] = [
                {"id": s["id"], "title": s["title"], "kind": s["kind"],
                 "score": s["score"]}
                for s in similarm.similar_documents(conn, doc_id, limit=8)]
        else:
            # documento nuovo, non ancora vettorizzato: calcola gli
            # embedding per attivare i simili semantici
            doc["similar_semantic"] = False
            doc["similar"] = []
        try:
            if have_embed:
                sugg = semanticm.suggest_groups_semantic(conn, doc_id)
                doc["group_suggestions"] = sugg if sugg else []
            else:
                doc["group_suggestions"] = semanticm.suggest_groups(
                    conn, doc_id)
        except Exception:
            doc["group_suggestions"] = []
        self._ok(doc)

    @staticmethod
    def _tags_of(conn, ids: list) -> dict:
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        out: dict[int, list] = {}
        for r in conn.execute(
                f"SELECT doc_id, tag FROM tags WHERE doc_id IN ({ph}) "
                "ORDER BY tag", ids):
            out.setdefault(r["doc_id"], []).append(r["tag"])
        return out

    @staticmethod
    def _stats(conn) -> dict:
        tot = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS s,"
                           " SUM(CASE WHEN text='' THEN 1 ELSE 0 END) AS notext"
                           " FROM documents").fetchone()
        kinds = [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM documents GROUP BY kind "
            "ORDER BY n DESC")]
        tags = [dict(r) for r in conn.execute(
            "SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag "
            "ORDER BY n DESC LIMIT 50")]
        dupes = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT sha256 FROM documents "
            "GROUP BY sha256 HAVING COUNT(*) > 1)").fetchone()["n"]
        return {"docs": tot["n"], "size": tot["s"], "no_text": tot["notext"],
                "kinds": kinds, "tags": tags, "dupes": dupes}

    # --- API POST (azioni sui documenti) --------------------------------------

    def _route_api_post_doc(self, doc_id: int, action: list, body: dict):
        conn = dbm.connect(self.db_path)
        if not conn.execute("SELECT 1 FROM documents WHERE id=?",
                            (doc_id,)).fetchone():
            self._err("Documento inesistente", 404)
            return
        if action == ["tag"]:
            tag = str(body.get("tag", "")).strip()
            if not tag:
                self._err("Tag vuoto")
                return
            if body.get("action") == "remove":
                conn.execute("DELETE FROM tags WHERE doc_id=? AND tag=?",
                             (doc_id, tag))
            else:
                conn.execute("INSERT OR IGNORE INTO tags(tag, doc_id) "
                             "VALUES(?,?)", (tag, doc_id))
            conn.commit()
            self._ok("fatto")
        elif action == ["group"]:
            name = str(body.get("group", "")).strip()
            if not name:
                self._err("Nome gruppo vuoto")
                return
            g = conn.execute("SELECT id, query FROM groups WHERE name=?",
                             (name,)).fetchone()
            if not g and body.get("create"):
                conn.execute("INSERT INTO groups(name) VALUES(?)", (name,))
                g = conn.execute("SELECT id, query FROM groups WHERE name=?",
                                 (name,)).fetchone()
            if not g:
                self._err(f"Gruppo inesistente: {name}", 404)
                return
            if g["query"]:
                self._err("È un gruppo smart: i membri sono calcolati "
                          "dalla query")
                return
            if body.get("action") == "remove":
                conn.execute("DELETE FROM doc_groups WHERE doc_id=? AND "
                             "group_id=?", (doc_id, g["id"]))
            else:
                conn.execute("INSERT OR IGNORE INTO doc_groups(doc_id, "
                             "group_id) VALUES(?,?)", (doc_id, g["id"]))
            conn.commit()
            self._ok("fatto")
        elif action == ["rm"]:
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            conn.commit()
            self._ok("fatto")
        else:
            self._err("Azione inesistente", 404)


# --- shell dell'applicazione (HTML+CSS+JS, nessuna dipendenza) -------------

GAPP_SHELL = r"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DocClassifier — ricerca documenti</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font:15px/1.5 -apple-system,'Segoe UI',Arial,sans-serif;
     background:#fff;color:#202124;min-height:100dvh;display:flex;
     flex-direction:column;align-items:center}
header{width:100%;display:flex;justify-content:flex-end;gap:14px;
       padding:14px 20px;font-size:13.5px}
header a{color:#444;text-decoration:none}
header a:hover{text-decoration:underline}
.logo{font-size:42px;font-weight:600;letter-spacing:-1px;margin:8vh 0 22px;
      text-align:center;user-select:none}
.l1{color:#4285f4}.l2{color:#ea4335}.l3{color:#fbbc05}.l4{color:#34a853}
.l5{color:#4285f4}.l6{color:#ea4335}.l7{color:#34a853}.l8{color:#4285f4}
.l9{color:#ea4335}.l10{color:#fbbc05}.l11{color:#34a853}.l12{color:#4285f4}
.sbox{display:flex;align-items:center;width:min(580px,92vw);
      border:1px solid #dfe1e5;border-radius:26px;padding:2px 8px 2px 20px;
      height:48px;transition:box-shadow .15s}
.sbox:focus-within{box-shadow:0 1px 8px rgba(32,33,36,.28);border-color:transparent}
.sbox input{flex:1;border:0;outline:0;font-size:17px;background:transparent;
            min-width:0}
.sbox button{border:0;background:none;font-size:17px;cursor:pointer;
             color:#4285f4;padding:8px 10px}
.hint{color:#70757a;font-size:13px;margin-top:12px;text-align:center;
      padding:0 14px}
.hint code{background:#f1f3f4;border-radius:4px;padding:1px 6px}
/* ---- chip parole chiave ---- */
#chips{width:min(720px,94vw);margin-top:14px;display:flex;gap:8px;
       flex-wrap:wrap;justify-content:center}
#fmtbar{width:min(720px,94vw);margin-top:16px;font-size:13px;color:#70757a;
        display:flex;gap:8px;align-items:center;flex-wrap:wrap;
        justify-content:center}
.flab{user-select:none}
.fchip{border:1px solid #dadce0;border-radius:14px;padding:4px 11px;
       cursor:pointer;user-select:none;background:#fff;color:#3c4043}
.fchip.on{background:#e6f4ea;border-color:#34a853;color:#137333;
          font-weight:600}
.fchip.all{border-style:dashed}
.chip{border:1px solid #dadce0;border-radius:18px;padding:7px 15px;
      font-size:14px;cursor:pointer;user-select:none;background:#fff;
      color:#3c4043}
.chip:hover{background:#f8f9fa}
.chip.plus{background:#e8f0fe;border-color:#aecbfa;color:#174ea6;
           font-weight:600}
.chip.minus{background:#fce8e6;border-color:#f6aea9;color:#a50e0e;
            font-weight:600;text-decoration:line-through}
.chip .st{font-weight:800;margin-left:4px}
/* ---- risultati ---- */
#results{width:min(680px,92vw);margin:26px 0 40px}
.res{margin-bottom:22px}
.res .t{font-size:18px;color:#1a0dab;cursor:pointer;word-break:break-word;
        line-height:1.3}
.res .t:hover{text-decoration:underline}
.res .u{font-size:13px;color:#0d652d;margin-top:1px;word-break:break-all}
.res .u .sc{color:#70757a}
.res .s{font-size:14px;color:#4d5156;margin-top:3px;word-break:break-word}
.nres{color:#70757a;font-size:13.5px;margin-bottom:18px}
.empty{color:#70757a;text-align:center;padding:30px 0}
/* ---- modale parole chiave ---- */
#kmodal{position:fixed;inset:0;background:rgba(32,33,36,.5);display:none;
        align-items:center;justify-content:center;z-index:9}
#kmodal.on{display:flex}
#kmodal .box{background:#fff;border-radius:12px;padding:22px;width:520px;
             max-width:94vw;max-height:88dvh;overflow-y:auto}
#kmodal h3{margin:0 0 10px}
#kmodal input,#kmodal textarea{width:100%;padding:10px 12px;font-size:15px;
  border:1px solid #dadce0;border-radius:8px;margin:6px 0;box-sizing:border-box;
  font-family:inherit}
#kmodal textarea{height:88px;resize:vertical}
#kmodal button{border:0;border-radius:8px;padding:10px 16px;font-size:14.5px;
  cursor:pointer;background:#1a73e8;color:#fff;font-weight:600}
#kmodal button.gray{background:#f1f3f4;color:#202124}
.krow{display:flex;align-items:center;gap:10px;padding:8px 2px;
      border-bottom:1px solid #eee;font-size:14px}
.krow .nm{font-weight:600;color:#1a73e8;min-width:110px}
.krow .tm{flex:1;color:#5f6368;font-size:12.5px;word-break:break-word}
.krow button{background:#f1f3f4;color:#a50e0e;padding:5px 10px;font-size:12.5px}
#status{position:fixed;left:12px;bottom:10px;font-size:11.5px;
        color:#9aa0a6;user-select:none;z-index:5}
#status .sc{color:#d8a413}
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:20px;
  background:#202124;color:#fff;padding:11px 20px;border-radius:8px;
  font-size:14px;opacity:0;transition:opacity .25s;max-width:88vw;z-index:30;
  text-align:center}
#toast.on{opacity:1}
</style></head><body>

<header>
  <a href="#" onclick="openKw()">⚙ Parole chiave</a>
  <a href="/classic">interfaccia completa →</a>
</header>

<div class="logo"><span class="l1">D</span><span class="l2">o</span><span class="l3">c</span><span class="l4">C</span><span class="l5">l</span><span class="l6">a</span><span class="l7">s</span><span class="l8">s</span><span class="l9">i</span><span class="l10">f</span><span class="l11">i</span><span class="l12">e</span><span class="l1">r</span></div>

<div class="sbox">
  <input id="q" placeholder="mamma assicurazione  ·  mamma carciofi -pomodoro"
         autocomplete="off">
  <button onclick="run()" title="cerca">🔍</button>
</div>
<div class="hint"><code>ricetta +#mamma +#carciofi -#pomodoro</code> —
  il vettore da cercare è la SOMMA dei termini: segno − = allontana ·
  <code>#nome</code> = parola chiave · pesi: <code>2*assicurazione</code></div>

<div id="fmtbar"><span class="flab">formati:</span><span id="fmt"></span></div>

<div id="chips"></div>
<div id="results"></div>

<div id="kmodal"><div class="box">
  <h3 style="margin:0 0 10px">Parole chiave e categorie</h3>
  <h4 class="sec" style="margin-top:0">📋 Categorie pronte all'uso</h4>
  <div class="mini" style="color:#5f6368;font-size:13px;margin-bottom:8px">
    Spunta le categorie della tua vita e creale in un colpo solo:
    i loro tag compaiono cliccabili nella pagina di ricerca.</div>
  <div id="presetlist"></div>
  <div style="display:flex;gap:8px;margin-top:10px">
    <button onclick="addPresets()">Crea le selezionate</button>
  </div>
  <h4 class="sec">Le tue parole chiave</h4>
  <div class="mini" style="color:#5f6368;font-size:13px;margin-bottom:10px">
    Una parola chiave è un <b>vettore composto</b>: frasi (peso 1),
    pesi <code>1.5*"frase"</code>, riferimenti <code>1.0*#altra</code> e
    <code>-1.0*#altra</code>, documenti campione
    <code>0.5*"D:\percorso\file.pdf"</code> — una voce per riga.
    Cercare <b>#nome</b> usa questo vettore.</div>
  <div id="klist"></div>
  <h3 style="margin-top:16px;font-size:16px">Nuova parola chiave</h3>
  <input id="kname" placeholder="nome, es. mamma" autocomplete="off">
  <textarea id="kterms" placeholder="mamma&#10;mama&#10;1.0*&quot;madre&quot;&#10;0.5*&quot;D:\foto\mamma.jpg&quot;&#10;-0.5*#fratello"></textarea>
  <div style="display:flex;gap:8px;margin-top:8px">
    <button onclick="saveKw()">Salva</button>
    <button class="gray" onclick="hideKw()">Chiudi</button>
  </div>
</div></div>

<div id="status">…</div>
<div id="toast"></div>

<script>
"use strict";
const $=s=>document.querySelector(s);
let KW=[];

/* ---- status indicizzazione (in basso a sinistra) ---- */
async function loadStatus(){
  try{
    const st=await api("/api/status");
    const t=new Date().toLocaleTimeString("it-IT",
      {hour:"2-digit",minute:"2-digit"});
    $("#status").innerHTML=
      '📄 '+st.docs+' documenti · 🧮 '+st.embeddings+' embeddings'+
      ' · aggiornato '+t+
      (st.scanning?' · <span class="sc">⏳ scansione in corso</span>':'');
  }catch(e){}
}
loadStatus();setInterval(loadStatus,10000);

async function api(path,opts){
  const r=await fetch(path,opts);
  const j=await r.json().catch(()=>({ok:false,error:"risposta non valida"}));
  if(!j.ok)throw new Error(j.error||("errore "+r.status));
  return j.data;
}
const post=(path,body)=>api(path,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});

function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("on"),3000);}

/* ---- filtro formati (memorizzato nel browser) ---- */
const ALLFMT=[".doc",".docx",".pdf",".xls",".xlsx",".ppt",".pptx",
              ".txt",".rtf",".odt"];
let FMT;
try{FMT=JSON.parse(localStorage.getItem("doccl-fmt"));}catch(e){}
if(!Array.isArray(FMT))FMT=[".doc",".docx"];   // default: solo Word
function saveFmt(){localStorage.setItem("doccl-fmt",JSON.stringify(FMT));}
function renderFmt(){
  const box=$("#fmt");box.innerHTML="";
  for(const f of ALLFMT){
    const c=document.createElement("span");
    c.className="fchip"+(FMT.includes(f)?" on":"");
    c.textContent=f;
    c.onclick=()=>{
      FMT=FMT.includes(f)?FMT.filter(x=>x!==f):FMT.concat(f);
      saveFmt();renderFmt();run();};
    box.appendChild(c);
  }
  const all=document.createElement("span");
  all.className="fchip all"+(FMT.length?"":" on");
  all.textContent="tutti";
  all.title="nessun filtro di formato";
  all.onclick=()=>{FMT=[];saveFmt();renderFmt();run();};
  box.appendChild(all);
}

/* ---- categorie pronte ---- */
let PRESETS=[],chosen=new Set();
async function loadPresets(){
  try{PRESETS=await api("/api/presets");}catch(e){PRESETS=[];}
  const box=$("#presetlist");box.innerHTML="";
  for(const p of PRESETS){
    const row=document.createElement("label");
    row.className="krow";row.style.cursor="pointer";
    const cb=document.createElement("input");cb.type="checkbox";
    cb.checked=p.exists;cb.disabled=p.exists;
    cb.onchange=()=>{cb.checked?chosen.add(p.name):chosen.delete(p.name);};
    row.appendChild(cb);
    const nm=document.createElement("span");nm.className="nm";
    nm.textContent="#"+p.name;row.appendChild(nm);
    const tm=document.createElement("span");tm.className="tm";
    tm.textContent=(p.terms||"").replace(/\n/g," · ")+
      (p.exists?"  (già creata)":"");
    row.appendChild(tm);
    box.appendChild(row);
  }
}
async function addPresets(){
  const names=[...chosen];
  if(!names.length){toast("Spunta almeno una categoria");return;}
  try{
    await post("/api/presets/add",{names});
    toast("✔ Create "+names.length+" categorie: "+names.join(", "));
    chosen.clear();
    await loadKW();await loadPresets();
  }catch(e){toast("⚠ "+e.message,true);}
}

/* ---- chip parole chiave ---- */
async function loadKW(){
  try{KW=await api("/api/keywords");}catch(e){KW=[];}
  const box=$("#chips");box.innerHTML="";
  if(!KW.length){
    const hint=document.createElement("span");
    hint.className="chip";hint.style.background="#f1f3f4";
    hint.textContent="Inizia scegliendo le categorie pronte → ⚙ Parole chiave";
    hint.onclick=openKw;box.appendChild(hint);}
  for(const k of KW){
    const c=document.createElement("span");
    c.className="chip";c.textContent="#"+k.name;
    c.onclick=()=>toggleKw(k.name);
    box.appendChild(c);
  }
}
function toggleKw(name){
  const q=$("#q");
  const toks=q.value.trim()?q.value.trim().split(/\s+/):[];
  const t="#"+name;
  const i=toks.indexOf(t);
  if(i>=0)toks.splice(i,1);
  else if(toks.includes("-"+t)){toks[toks.indexOf("-"+t)]=t;}
  else toks.push(t);
  q.value=toks.join(" ");
  paintChips();
  run();
}
function paintChips(){
  const toks=$("#q").value.trim().split(/\s+/);
  document.querySelectorAll("#chips .chip").forEach(c=>{
    const name=c.textContent;   // "#mamma"
    c.classList.remove("plus","minus");
    if(toks.includes(name)){c.classList.add("plus");
      c.innerHTML=name+'<span class="st">+</span>';}
    else if(toks.includes("-"+name)){c.classList.add("minus");
      c.innerHTML=name+'<span class="st">−</span>';}
    else c.textContent=name;
  });
}

/* ---- ricerca ---- */
async function run(){
  const q=$("#q").value.trim();
  paintChips();
  const box=$("#results");
  if(!q){box.innerHTML="";return;}
  box.innerHTML='<div class="empty">Ricerca…</div>';
  try{
    const d=await api("/api/gsearch?q="+encodeURIComponent(q));
    let rows=d.results;
    // il filtro esclude solo i file con estensione nota e non selezionata:
    // i file senza estensione passano sempre
    if(FMT.length)rows=rows.filter(r=>!(r.ext||"")||
      FMT.includes(r.ext.toLowerCase()));
    if(!rows.length){
      box.innerHTML='<div class="empty">Nessun documento per <b>'+
        d.q+'</b>'+(FMT.length&&d.results.length?
        " con i formati selezionati ("+d.results.length+" senza filtro)":
        ".<br>Prova con meno termini, o senza il segno −.")+'</div>';
      return;}
    const head=document.createElement("div");
    head.className="nres";
    head.textContent=(FMT.length?rows.length+" di "+d.results.length+" — ":"")+
      d.n+" documenti (ordine: più vicini al vettore della ricerca)";
    box.appendChild(head);
    for(const r of rows){
      const div=document.createElement("div");div.className="res";
      const t=document.createElement("div");t.className="t";
      t.textContent=r.title||r.filename;
      t.onclick=()=>window.open("/file/"+r.id,"_blank");
      div.appendChild(t);
      const u=document.createElement("div");u.className="u";
      u.innerHTML='<span class="sc">'+r.score.toFixed(2)+' · </span>'+
        r.kind+" · "+(r.date||"");
      div.appendChild(u);
      const p=document.createElement("div");p.className="u";
      p.textContent=r.path;div.appendChild(p);
      const s=document.createElement("div");s.className="s";
      s.textContent=r.snippet;div.appendChild(s);
      box.appendChild(div);
    }
  }catch(e){box.innerHTML='<div class="empty">⚠ '+e.message+'</div>';}
}
$("#q").addEventListener("keydown",e=>{if(e.key==="Enter")run();});
$("#q").addEventListener("input",paintChips);

/* ---- gestione parole chiave ---- */
function openKw(){$("#kmodal").classList.add("on");loadKwList();loadPresets();}
function hideKw(){$("#kmodal").classList.remove("on");}
async function loadKwList(){
  const box=$("#klist");box.innerHTML="";
  for(const k of KW){
    const row=document.createElement("div");row.className="krow";
    const nm=document.createElement("span");nm.className="nm";
    nm.textContent="#"+k.name;row.appendChild(nm);
    const tm=document.createElement("span");tm.className="tm";
    tm.textContent=(k.terms||"").replace(/\n/g," · ");row.appendChild(tm);
    const rm=document.createElement("button");rm.textContent="Elimina";
    rm.onclick=async()=>{await post("/api/keywords/remove",{name:k.name});
      toast("Parola chiave eliminata");await loadKW();await loadKwList();};
    row.appendChild(rm);box.appendChild(row);
  }
  if(!KW.length)box.innerHTML='<div class="mini" style="color:#5f6368">— nessuna parola chiave —</div>';
}
async function saveKw(){
  const name=$("#kname").value.trim().replace(/^#/,"");
  const terms=$("#kterms").value.trim();
  if(!name||!terms){toast("Servono nome e termini");return;}
  try{await post("/api/keywords/save",{name,terms});
    toast("Parola chiave #"+name+" salvata");
    $("#kname").value="";$("#kterms").value="";
    await loadKW();await loadKwList();}
  catch(e){toast("⚠ "+e.message);}
}

renderFmt();loadKW();
</script></body></html>"""

APP_SHELL = r"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>devon — archivio documentale</title>
<style>
:root{--bg:#f2efe9;--panel:#fff;--ink:#22262a;--mut:#7a7f85;--line:#e2ded5;
      --side:#232c33;--side-ink:#cfd6dc;--acc:#9a6b1f;--acc-bg:#f6e8cf;
      --blue:#1a5276;--red:#9c2b2b;}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,'Segoe UI',sans-serif;
     background:var(--bg);color:var(--ink);display:flex;height:100vh;
     overflow:hidden}
/* ---- barra laterale ---- */
aside{width:230px;background:var(--side);color:var(--side-ink);
      padding:14px 12px;overflow-y:auto;flex-shrink:0}
aside h1{font-size:17px;margin:0 0 2px;color:#fff}
aside .sub{font-size:11px;color:#8b959d;margin-bottom:14px}
aside h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;
         color:#8b959d;margin:18px 0 6px;border-bottom:1px solid #37424b;
         padding-bottom:4px}
aside .item{display:block;padding:5px 8px;border-radius:6px;cursor:pointer;
            color:var(--side-ink);text-decoration:none;font-size:13px;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
aside .item:hover{background:#33404a;color:#fff}
aside .item.sel{background:var(--acc);color:#fff}
aside .item .n{float:right;color:#8b959d;font-size:11px;margin-left:6px}
aside .item.sel .n{color:#f3e2c2}
button{font:inherit;border:0;border-radius:6px;cursor:pointer}
.btn{background:var(--acc-bg);color:var(--acc);padding:8px 14px;
     font-weight:600;width:100%;margin-top:6px}
.btn:hover{filter:brightness(.95)}
/* ---- colonna centrale: lista ---- */
main{flex:1;display:flex;flex-direction:column;min-width:0}
.searchbar{padding:12px 16px;background:var(--panel);
           border-bottom:1px solid var(--line);display:flex;gap:10px}
.searchbar input{flex:1;padding:10px 14px;font-size:15px;border:1px solid
                 var(--line);border-radius:8px;background:var(--bg)}
.searchbar .hint{align-self:center;color:var(--mut);font-size:12px;
                 max-width:220px}
#list{flex:1;overflow-y:auto;padding:12px 16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
      padding:12px 14px;margin-bottom:10px;cursor:pointer}
.card:hover{border-color:var(--acc)}
.card.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.card h3{margin:0 0 3px;font-size:14.5px;color:var(--blue);
         word-break:break-word}
.card .meta{color:var(--mut);font-size:12px}
.card .snip{margin-top:6px;color:#43484d;font-size:13px}
.card .path{font:11px ui-monospace,monospace;color:#9aa0a6;margin-top:5px;
            word-break:break-all}
.tag{display:inline-block;background:#e8f0fe;color:var(--blue);
     border-radius:11px;padding:1px 9px;margin:3px 3px 0 0;font-size:12px;
     cursor:pointer}
.tag:hover{background:#c8dcfa}
/* ---- colonna destra: dettaglio (visibile solo con documento aperto) ---- */
#detail{display:none;width:420px;background:var(--panel);
        border-left:1px solid var(--line);overflow-y:auto;padding:18px;
        flex-shrink:0}
#detail.on{display:block}
#detail .close{float:right;color:var(--mut);background:none;font-size:18px;
               padding:0 6px}
#detail h2{margin:2px 30px 8px 0;font-size:17px;word-break:break-word}
#detail dl{margin:0 0 14px;font-size:13px}
#detail dt{color:var(--mut);float:left;clear:left;width:86px}
#detail dd{margin:0 0 3px 96px;word-break:break-all}
.chipline{margin:6px 0}
.chipline .tag .x{color:#b33;margin-left:5px;font-weight:700}
.editrow{display:flex;gap:6px;margin:8px 0}
.editrow input,.editrow select{flex:1;padding:6px 9px;font-size:13px;
     border:1px solid var(--line);border-radius:6px;background:#fff;
     min-width:0}
.editrow button{background:#eef1f4;padding:6px 12px;color:var(--ink)}
.mini{font-size:11px;color:var(--mut)}
.actions{display:flex;gap:8px;margin:12px 0}
.actions a,.actions button{flex:1;text-align:center;padding:8px 4px;
     border-radius:6px;text-decoration:none;font-size:13px}
.open{background:var(--acc-bg);color:var(--acc)!important;font-weight:600}
.danger{background:#f7e4e4;color:var(--red)!important}
pre.text{background:#faf9f5;border:1px solid var(--line);padding:12px;
         white-space:pre-wrap;font:12px/1.5 ui-monospace,monospace;
         max-height:340px;overflow-y:auto}
.sim{padding:6px 8px;border-radius:6px;cursor:pointer;margin-bottom:4px;
     background:#f7f6f2}
.sim:hover{background:var(--acc-bg)}
.sim .sc{color:var(--acc);font-weight:700;font-size:12px;margin-right:6px}
h4.sec{margin:18px 0 6px;border-bottom:1px solid var(--line);
       padding-bottom:4px;font-size:12px;text-transform:uppercase;
       letter-spacing:.06em;color:var(--mut)}
.qtag{display:inline-block;background:#f0ede6;color:#6b5d2e;border-radius:11px;
  padding:1px 9px;margin:3px 3px 0 0;font-size:12px;cursor:pointer;border:0}
.qtag:hover{background:#e6e0d0}

/* ---- modale import ---- */
#modal,.modal2{position:fixed;inset:0;background:rgba(20,24,28,.45);
       display:none;align-items:center;justify-content:center;z-index:6}
#modal.on,.modal2.on{display:flex}
#modal .box,.modal2 .box{background:#fff;border-radius:10px;padding:22px;
            width:440px;max-width:92vw;max-height:86vh;overflow-y:auto}
#modal .box.wide{width:560px}
#modal input,.modal2 input[type=text]{width:100%;padding:9px 11px;font-size:14px;border:1px solid
             var(--line);border-radius:6px;margin:8px 0}
.srcrow{display:flex;align-items:center;gap:8px;padding:8px 6px;
        border-bottom:1px solid var(--line);font-size:13px}
.srcrow .p{flex:1;min-width:0;word-break:break-all}
.srcrow .p small{color:var(--mut);display:block;font-size:11px}
.srcrow button{padding:4px 10px;font-size:12px;border-radius:5px;
               background:#eef1f4;cursor:pointer;border:0}
.srcrow button.upd{background:var(--acc-bg);color:var(--acc);font-weight:600}
.sem{display:flex;align-items:center;gap:5px;white-space:nowrap;
     color:var(--mut);font-size:13px;align-self:center;cursor:pointer;
     user-select:none}
.sem input{margin:0}
.sc{color:var(--acc);font-weight:700;font-size:11px;background:var(--acc-bg);
    border-radius:8px;padding:1px 7px;margin-right:6px;white-space:nowrap}
.card .acts{display:flex;gap:6px;margin-top:7px}
.card .acts button{font-size:11.5px;padding:3px 10px;border-radius:5px;
  background:#eef1f4;cursor:pointer;border:0;color:var(--ink)}
.card .acts button.pos{background:var(--acc-bg);color:var(--acc);
  font-weight:700}
.card .acts button.neg{background:#f7e4e4;color:var(--red);font-weight:700}
.card .acts button:hover{filter:brightness(.95)}
.cfilters{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.cfilters input{flex:1;min-width:160px;padding:7px 10px;font-size:13px;
  border:1px solid var(--line);border-radius:6px}
.cfilters select{padding:7px 8px;font-size:13px;border:1px solid var(--line);
  border-radius:6px;background:#fff}
#csel{padding:9px 8px;font-size:14px;border:1px solid var(--line);
  border-radius:8px;background:#fff;color:var(--ink);max-width:210px;
  align-self:center}
#csel:not([value=""]){background:var(--acc-bg);color:var(--acc);
  font-weight:600}
#modal .bar{height:8px;background:#eee;border-radius:4px;overflow:hidden;
            margin:10px 0}
#modal .bar i{display:block;height:100%;background:var(--acc);width:0%}
.cgrid{display:flex;gap:12px;margin-top:8px}
.cgrid>div{flex:1;min-width:0}
.cgrid textarea{width:100%;height:64px;padding:7px 9px;
  font:13px/1.4 Arial,sans-serif;border:1px solid var(--line);
  border-radius:6px;box-sizing:border-box;resize:vertical}
.cgrid input{width:100%;padding:7px 9px;font-size:13px;border:1px solid
  var(--line);border-radius:6px;box-sizing:border-box;margin:4px 0}
#toast{position:fixed;right:16px;bottom:16px;background:#2c353d;color:#fff;
       padding:10px 16px;border-radius:8px;font-size:13px;opacity:0;
       transition:opacity .25s;max-width:340px}
#toast.on{opacity:1}
.empty{color:var(--mut);text-align:center;padding:40px 10px}
#scanstrip{display:none;background:#fff6dd;color:#7a5c12;
  border-bottom:1px solid #eddfb3;padding:7px 16px;font-size:13px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#scanstrip.on{display:block}
#scanstrip .dot{display:inline-block;width:9px;height:9px;border-radius:50%;
  background:#d8a413;margin-right:7px;animation:pulse 1.1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
@media (max-width:1000px){#detail.on{position:fixed;right:0;top:0;bottom:0;
  width:min(420px,94vw);box-shadow:-4px 0 18px rgba(0,0,0,.15);z-index:5}}
/* ---- topbar e comportamento smartphone ---- */
#topbar{display:none;position:fixed;top:0;left:0;right:0;z-index:20;
        background:var(--side);color:#fff;height:52px;
        align-items:center;gap:8px;padding:0 10px}
#topbar .logo{font-weight:700;font-size:16px}
#burger{background:none;border:0;color:#fff;font-size:22px;padding:6px 12px;
        cursor:pointer;line-height:1}
#scrim{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);
       z-index:19}
#scrim.on{display:block}
@media (max-width:760px){
  body{font-size:15px;padding-top:52px}
  #topbar{display:flex}
  aside{position:fixed;top:52px;bottom:0;left:0;width:min(280px,86vw);
        z-index:19;transform:translateX(-102%);transition:transform .22s;
        padding-top:8px}
  aside.open{transform:none;box-shadow:4px 0 18px rgba(0,0,0,.25)}
  .searchbar{padding:10px 12px}
  .searchbar .hint{display:none}
  .searchbar input{min-width:0;font-size:16px}
  #list{padding:10px}
  .card{padding:11px 12px}
  .card h3{font-size:15.5px}
  #detail.on{position:fixed;inset:52px 0 0 0;width:auto;z-index:18;
             border-left:0;box-shadow:none}
  .cgrid{flex-direction:column}
  .cgrid>div{min-width:0}
  .actions a,.actions button{min-width:0}
}
</style></head><body>
<div id="topbar"><button id="burger" aria-label="menu">☰</button>
<span class="logo">📚 devon</span></div>
<div id="scrim"></div>

<aside>
  <h1>📚 devon</h1>
  <div class="sub">archivio documentale</div>
  <div class="item sel" data-view="cats">🏷 Categorie</div>
  <div class="item" data-q="">Tutti i documenti <span class="n" id="n-all"></span></div>
  <div class="item" data-view="dupes">♻ Duplicati <span class="n" id="n-dupes"></span></div>
  <button class="btn" onclick="showSources()">📂 Cartelle e indice…</button>
  <button class="btn" onclick="showAnalysis()">🧠 Analisi semantica…</button>
  <button class="btn" onclick="showConcepts()">🏷️ Concetti…</button>
  <h2>Tipi</h2><div id="kinds"></div>
  <h2>Argomenti</h2><div id="topics"></div>
  <h2>Concetti</h2><div id="concepts"></div>
  <h2>Gruppi</h2><div id="groups"></div>
  <h2>Tag</h2><div id="tags"></div>
</aside>

<main>
  <div class="searchbar">
    <input id="q" placeholder="Cerca… (consigl* tag:personale kind:pdf -bozza)"
           autofocus autocomplete="off">
    <label class="sem" title="Cerca per significato, non solo per parola">
      <input type="checkbox" id="sem"> semantica</label>
    <select id="csel" title="Classifica per concetto">
      <option value="">🏷️ concetto: tutti</option>
    </select>
    <div class="hint">tag: · kind: · ext: · path: · group: · topic: ·
      prefisso* · -escludi</div>
  </div>
  <div id="scanstrip">⏳ Scansione in corso…</div>
  <div id="list"></div>
</main>

<div id="detail"></div>

<div id="modal"><div class="box wide">
  <h3 style="margin-top:0">Cartelle e indice</h3>
  <p class="mini" style="margin:4px 0 6px">Percorsi da indicizzare. «Aggiorna»
  rileva i file <b>nuovi e modificati</b> (chi modifica un file lo
  re-indicizza; i file immutati vengono saltati in un attimo). Rimuovere
  una cartella NON cancella i documenti già indicizzati.</p>
  <div style="display:flex;gap:8px">
    <input id="spath" type="text" placeholder="es. D:\Documenti"
           autocomplete="off" style="flex:1">
    <button class="btn" style="margin:0;width:auto" onclick="addSource()">
      Aggiungi</button>
  </div>
  <div id="srclist"></div>
  <div style="display:flex;gap:8px;margin-top:10px">
    <button class="btn" style="margin:0" onclick="scanAllSources()">
      ↻ Aggiorna tutto l'indice</button>
    <button style="background:#eef1f4;padding:8px 14px" onclick="hideModal()">
      Chiudi</button>
  </div>
  <div class="bar"><i id="ibar"></i></div>
  <div class="mini" id="istatus">indice pronto</div>
</div></div>

<div id="amodal" class="modal2"><div class="box">
  <h3 style="margin-top:0">Analisi semantica</h3>
  <p class="mini" style="margin:4px 0 8px">Tutto il calcolo avviene in
  locale: nessun documento lascia il computer.</p>
  <div class="mini" id="astate"></div>
  <div style="display:flex;gap:8px;align-items:center;margin:10px 0">
    <button class="btn" style="margin:0" onclick="doEmbed()">Calcola embedding</button>
    <button class="btn" style="margin:0" onclick="doCluster()">Raggruppa</button>
    <span class="mini">k =</span>
    <input id="ak" type="number" value="40" min="2" max="200"
           style="width:64px;padding:5px 7px;border:1px solid var(--line);
           border-radius:6px">
    <button style="background:#eef1f4;padding:8px 12px"
            onclick="hideA()">Chiudi</button>
  </div>
  <div class="bar"><i id="abar"></i></div>
  <div class="mini" id="astatus"></div>
  <p class="mini" id="anote" style="margin-top:8px"></p>
</div></div>

<div id="cmodal" class="modal2"><div class="box wide">
  <div style="display:flex;align-items:center;gap:10px">
    <h3 style="margin:8px 0">Categorie (concetti)</h3>
    <button class="btn" style="margin:0;width:auto"
            onclick="newConceptForm()">＋ Nuova categoria</button>
  </div>
  <p class="mini" style="margin:4px 0 8px">Un concetto classifica i
  documenti per prossimità: l'ancoraggio <b>A</b> (parole/frasi, documenti
  campione, tipi file) avvicina, l'ancoraggio <b>B</b> allontana. Ogni
  documento XX ottiene punteggio = sim(XX,A) − sim(XX,B): chi supera la
  soglia è classificato. Una riga <b>@Nome</b> richiama un altro concetto
  o una categoria sem: (anche come negativo): il concetto diventa così
  combinazione di categorie.</p>
  <div id="clist"></div>
  <h4 style="margin:14px 0 4px">Nuovo / modifica concetto</h4>
  <input id="cname" type="text" placeholder="nome, es. famiglia"
         autocomplete="off">
  <div class="cgrid">
    <div><b class="mini">A · positivo</b>
      <textarea id="cpostext" placeholder="parole o frasi, una per riga&#10;mamma&#10;fratello&#10;@AltroConcetto"></textarea>
      <input id="cposdocs" type="text" placeholder="campioni positivi: percorso completo, es. D:\Documenti\lettera.docx"
             autocomplete="off">
      <input id="cposexts" type="text" placeholder="tipi: .docx .xlsx .doc .ppt .txt"
             autocomplete="off">
    </div>
    <div><b class="mini">B · negativo</b>
      <textarea id="cnegtext" placeholder="frasi o @Categoria da allontanare&#10;inferenza&#10;@Honda"></textarea>
      <input id="cnegdocs" type="text" placeholder="campioni negativi: percorso completo, es. C:\Lavoro\report.py"
             autocomplete="off">
      <input id="cnegexts" type="text" placeholder="tipi: .c .py .sh"
             autocomplete="off">
    </div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
    <label class="sem" title="Con «solo questi tipi» i documenti di altre
      estensioni vengono esclusi, non solo favoriti">
      <input type="checkbox" id="cstrict"> tipi come filtro obbligatorio
    </label>
    <span class="mini">soglia</span>
    <input id="cthr" type="number" step="0.01" value="0.10"
           style="width:76px">
    <button class="btn" style="margin:0" onclick="saveConcept()">Salva</button>
    <button style="background:#eef1f4;padding:8px 12px" onclick="hideC()">
      Chiudi</button>
  </div>
</div></div>

<div id="qmodal" class="modal2"><div class="box">
  <h3 style="margin-top:0">Aggiungi a un concetto</h3>
  <div class="mini" id="qdoc"></div>
  <select id="qconcept" style="width:100%;padding:8px;margin:8px 0;font-size:14px"></select>
  <div style="display:flex;gap:8px">
    <button class="btn" style="margin:0" onclick="quickAdd('pos')">
      Positivo (A)</button>
    <button class="btn" style="margin:0;background:#f7e4e4;color:var(--red)"
            onclick="quickAdd('neg')">Negativo (B)</button>
    <button style="background:#eef1f4;padding:8px 12px"
            onclick="hideQ()">Annulla</button>
  </div>
</div></div>

<div id="toast"></div>

<script>
"use strict";
const $=s=>document.querySelector(s);
let state={q:"",view:"list",selId:null,groups:[],stats:null,doc:null};

/* ---- chiamate API ---- */
async function api(path,opts){
  const r=await fetch(path,opts);
  const j=await r.json().catch(()=>({ok:false,error:"risposta non valida"}));
  if(!j.ok)throw new Error(j.error||("errore "+r.status));
  return j.data;
}
const post=(path,body)=>api(path,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});

function toast(msg,long){
  const t=$("#toast");t.textContent=msg;t.classList.add("on");
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("on"),
    long?7000:2600);}

/* ---- ricerca ---- */
let debounce;
function onSearch(){
  clearTimeout(debounce);
  debounce=setTimeout(async()=>{
    state.q=$("#q").value.trim();
    // dentro una categoria la barra filtra i RISULTATI della categoria:
    // kind:/ext: scelgono la tipologia, il resto filtra per parola chiave
    if(state.view==="concept"){
      const cf=state.cf||{};
      const rest=[];
      cf.type="";
      for(const tok of state.q.split(/\s+/)){
        const m=tok.match(/^(kind|ext|tipo):(.*)$/i);
        if(m&&m[2]){cf.type=m[2].replace(/^\.*/,"");}
        else if(tok){rest.push(tok);}
      }
      cf.q=rest.join(" ").toLowerCase();
      renderConceptList();
      return;
    }
    state.view="list";markSidebar();
    await loadList();
  },250);
}

async function loadList(){
  const box=$("#list");
  if(state.view!=="concept")$("#csel").value="";
  if(state.view==="concept"){
    box.innerHTML='<div class="empty" id="cload">Classificazione in corso…</div>';
    const t0=Date.now();
    const ct=setInterval(()=>{
      const el2=document.getElementById("cload");
      if(!el2){clearInterval(ct);return;}
      el2.textContent="Classificazione in corso… "+
        Math.round((Date.now()-t0)/1000)+"s"+
        " (se è la prima dopo l'avvio, sto caricando i vettori)";
    },1000);
    try{
      const d=await api("/api/concept/get/"+state.conceptId);
      clearInterval(ct);
      state.conceptData=d;state.cf={q:"",type:""};
      renderConceptList();
    }catch(e){clearInterval(ct);box.innerHTML="";toast("⚠ "+e.message);}
    return;}
  const sem=$("#sem").checked;
  if(sem&&!state.q.trim()){
    box.innerHTML='<div class="empty">Scrivi una frase intera: la ricerca '+
      'semantica la confronta con il <b>significato</b> dei documenti.</div>';
    return;}
  box.innerHTML='<div class="empty">Ricerca in corso…</div>';
  try{
    const url=sem?"/api/semantic?q="+encodeURIComponent(state.q)
                 :"/api/search?q="+encodeURIComponent(state.q);
    const docs=await api(url);
    if(!docs.length){box.innerHTML='<div class="empty">Nessun documento.'+
      (state.q?" Prova con altre parole, un prefisso (es. consigl*) o la "+
      "ricerca semantica.":" Importa una cartella con il pulsante nella "+
      "barra laterale.")+'</div>';}
    box.innerHTML="";
    for(const d of docs)box.appendChild(card(d));
  }catch(e){box.innerHTML="";toast("⚠ "+e.message);}
}

function el(tag,cls,text){const n=document.createElement(tag);
  if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;}

function snippetNode(snip){
  const div=el("div","snip");
  for(const part of String(snip).split(/≪|≫/)){
    if(div._m){const m=document.createElement("mark");m.textContent=part;
      div.appendChild(m);div._m=false;}
    else{div.appendChild(document.createTextNode(part));div._m=true;}
  }
  return div.childNodes.length?div:null;
}

function card(d){
  const c=el("div","card"+(state.selId===d.id?" sel":""));
  c.onclick=()=>selectDoc(d.id);
  c.appendChild(el("h3",null,`${d.title||d.filename}`));
  const meta=el("div","meta");
  if(d.score!=null)meta.appendChild(el("span","sc",d.score.toFixed(2)));
  meta.appendChild(document.createTextNode(
    `${d.kind} · ${d.date||"—"} · ${fmtSize(d.size)} · ${d.filename}`));
  if(d.sim_w!=null)meta.appendChild(document.createTextNode(
    ` · P ${d.sim_w.toFixed(2)} · D ${d.sim_d.toFixed(2)} · B −${d.sim_b.toFixed(2)}`));
  else if(d.sim_a!=null)meta.appendChild(document.createTextNode(
    ` · A ${d.sim_a.toFixed(2)} / B ${d.sim_b.toFixed(2)}`));
  c.appendChild(meta);
  const s=snippetNode(d.snippet);if(s)c.appendChild(s);
  const tw=el("div");
  for(const t of (d.tags||[])){
    const tag=el("span","tag",t);
    tag.onclick=e=>{e.stopPropagation();addQuery("tag:"+t);};
    tw.appendChild(tag);
  }
  if(state.view!=="dupes"&&(state.concepts||[]).length){
    const qb=el("button","qtag","🏷 concetto…");
    qb.title="aggiungi questo documento a un concetto come esempio A o B";
    qb.onclick=e=>{e.stopPropagation();quickConcept(d.id,d.title);};
    tw.appendChild(qb);
  }
  c.appendChild(tw);
  c.appendChild(el("div","path",d.path));
  return c;
}

function addQuery(frag){
  const q=$("#q").value.trim().split(/\s+/).filter(Boolean);
  if(!q.includes(frag))q.push(frag);
  $("#q").value=q.join(" ");onSearch();
}

/* ---- dettaglio documento ---- */
async function selectDoc(id){
  state.selId=id;
  document.querySelectorAll(".card").forEach(c=>c.classList.remove("sel"));
  try{state.doc=await api("/api/doc/"+id);renderDetail();}
  catch(e){toast("⚠ "+e.message);}
}

function renderDetail(){
  const d=state.doc,box=$("#detail");box.innerHTML="";
  box.classList.add("on");
  const x=el("button","close","×");x.onclick=closeDetail;box.appendChild(x);
  box.appendChild(el("h2",null,d.title||d.filename));

  const act=el("div","actions");
  const open=el("a","open",d.exists?"Apri file ↗":"File mancante");
  open.href=d.exists?"/file/"+d.id:"#";open.target="_blank";
  if(!d.exists){open.className="mini";open.onclick=e=>e.preventDefault();}
  act.appendChild(open);
  const dl=el("a",null,"Scarica");dl.href="/file/"+d.id+"?download=1";
  dl.target="_blank";dl.rel="noopener";
  dl.className="mini";dl.style.alignSelf="center";act.appendChild(dl);
  const rm=el("button","danger","Rimuovi dall'indice");
  rm.onclick=()=>removeDoc(d.id);act.appendChild(rm);
  box.appendChild(act);

  const dl2=el("dl");
  const rows=[["tipo",`${d.kind} (${d.ext})`],["dimensione",fmtSize(d.size)],
    ["data file",d.date||"—"],["autore",d.author||"—"],
    ["argomento",d.topic||"—"],
    ["indicizzato",d.added||"—"],["percorso",d.path]];
  for(const[k,v]of rows){dl2.appendChild(el("dt",null,k));
    dl2.appendChild(el("dd",null,v));}
  box.appendChild(dl2);

  box.appendChild(el("h4","sec","Tag"));
  const line=el("div","chipline");
  for(const t of d.tags){
    const tag=el("span","tag");
    tag.appendChild(document.createTextNode(t));
    const x2=el("span","x","×");x2.onclick=()=>setTag(t,"remove");
    tag.appendChild(x2);
    tag.onclick=e=>{if(e.target===tag)addQuery("tag:"+t);};
    line.appendChild(tag);
  }
  if(!d.tags.length)line.appendChild(el("span","mini","nessuno"));
  box.appendChild(line);
  const tr=el("div","editrow");
  const ti=el("input");ti.placeholder="nuovo tag…";ti.id="ntag";
  const tb=el("button",null,"Aggiungi");tb.onclick=()=>{
    if(ti.value.trim())setTag(ti.value.trim(),"add");};
  tr.append(ti,tb);box.appendChild(tr);

  box.appendChild(el("h4","sec","Gruppi"));
  const gl=el("div","chipline");
  for(const g of d.groups){
    const tag=el("span","tag",g.name+" ×");
    tag.onclick=()=>setGroup(g.name,"remove");gl.appendChild(tag);
  }
  for(const s of (d.smart_groups||[]))
    gl.appendChild(el("span","tag",s+" ⚡"));
  if(!d.groups.length&&!(d.smart_groups||[]).length)
    gl.appendChild(el("span","mini","nessuno"));
  box.appendChild(gl);
  const gr=el("div","editrow");
  const gs=el("select");
  gs.appendChild(new Option("— scegli gruppo —",""));
  const used=new Set(d.groups.map(g=>g.name));
  for(const g of state.groups)
    if(!g.query&&!used.has(g.name))gs.appendChild(new Option(g.name,g.name));
  gs.appendChild(new Option("➕ crea nuovo…","__new__"));
  const gb=el("button",null,"Aggiungi");
  gb.onclick=()=>{
    if(gs.value==="__new__")setGroup($("#ngroup").value.trim(),"add",true);
    else if(gs.value)setGroup(gs.value,"add");};
  gr.append(gs,gb);box.appendChild(gr);
  const gr2=el("div","editrow");gr2.id="newgrouprow";gr2.style.display="none";
  const gi=el("input");gi.id="ngroup";gi.placeholder="nome nuovo gruppo…";
  gr2.appendChild(gi);box.appendChild(gr2);
  gs.onchange=()=>{gr2.style.display=gs.value==="__new__"?"flex":"none";};

  if((d.group_suggestions||[]).length){
    box.appendChild(el("h4","sec","Gruppi suggeriti (per contenuto)"));
    const sl=el("div","chipline");
    for(const s of d.group_suggestions){
      const c=el("span","tag",
        `${s.name} · ${s.score.toFixed(2)}`);
      c.title="Clic per mettere il documento in questo gruppo";
      c.onclick=()=>setGroup(s.name,"add");
      sl.appendChild(c);
    }
    box.appendChild(sl);
  }

  if((state.concepts||[]).length){
    box.appendChild(el("h4","sec","Concetti: usa come esempio"));
    const r=el("div","editrow");
    const cs=el("select");
    cs.appendChild(new Option("— scegli concetto —",""));
    for(const cpt of state.concepts)
      cs.appendChild(new Option(cpt.name,cpt.id));
    const ba=el("button",null,"A +");
    ba.title="aggiungi questo documento all'ancoraggio positivo";
    ba.onclick=()=>cs.value&&addSampleDoc(cs.value,"pos");
    const bb=el("button",null,"B −");
    bb.title="aggiungi questo documento all'ancoraggio negativo";
    bb.onclick=()=>cs.value&&addSampleDoc(cs.value,"neg");
    r.append(cs,ba,bb);box.appendChild(r);
  }

  if(d.text){
    box.appendChild(el("h4","sec","Testo"));
    const pre=el("pre","text",d.text.slice(0,12000));
    box.appendChild(pre);
  }else box.appendChild(el("p","mini","Nessun testo estraibile (solo metadati)."));

  if(d.similar&&d.similar.length){
    box.appendChild(el("h4","sec",d.similar_semantic
      ?"Documenti simili (semantici)":"Documenti simili (per parole)"));
    for(const s of d.similar){
      const row=el("div","sim");
      row.appendChild(el("span","sc",s.score.toFixed(2)));
      row.appendChild(document.createTextNode(s.title));
      row.onclick=()=>selectDoc(s.id);
      box.appendChild(row);
    }
  }
}

function closeDetail(){state.selId=null;state.doc=null;
  $("#detail").classList.remove("on");$("#detail").innerHTML="";}

async function setTag(tag,action){
  try{await post(`/api/doc/${state.selId}/tag`,{tag,action});
    toast(action==="add"?"Tag aggiunto: "+tag:"Tag rimosso: "+tag);
    await selectDoc(state.selId);await loadSidebar();await loadList();}
  catch(e){toast("⚠ "+e.message);}
}

async function setGroup(name,action,create){
  try{await post(`/api/doc/${state.selId}/group`,{group:name,action,create});
    toast("Gruppo aggiornato.");await loadSidebar();
    await selectDoc(state.selId);}
  catch(e){toast("⚠ "+e.message);}
}

async function removeDoc(id){
  if(!confirm("Rimuovere il documento dall'indice?\n"+
    "(il file su disco NON viene toccato)"))return;
  try{await post(`/api/doc/${id}/rm`,{});
    toast("Rimosso dall'indice.");closeDetail();await loadList();
    await loadSidebar();}
  catch(e){toast("⚠ "+e.message);}
}

function renderConceptList(){
  const box=$("#list");
  const d=state.conceptData,c=d.concept;
  box.innerHTML="";
  const head=el("div","card");head.style.cursor="default";
  head.appendChild(el("h3",null,"Concetto: "+c.name));
  head.appendChild(el("div","meta",
    d.total+" documenti classificati"+
    (c.ext_mode==="solo"?" · SOLO tipi "+c.pos_exts:"")));
  head.appendChild(el("div","meta","A-parole: "+
    (c.pos_text||"—").replace(/\n/g,"; ")+
    (c.pos_docs?"  · A-documenti: "+c.pos_docs:"")+
    (c.pos_exts?"  · tipi "+c.pos_exts:"")));
  if(c.neg_text||c.neg_docs||c.neg_exts)
    head.appendChild(el("div","meta","B: "+
      (c.neg_text||"—").replace(/\n/g,"; ")+
      (c.neg_docs?"  + campioni":"")+
      (c.neg_exts?"  ["+c.neg_exts+"]":"")));
  const thr=el("div","editrow");
  thr.appendChild(el("span","mini","soglia:"));
  const ti=el("input");ti.type="number";ti.step="0.01";
  ti.value=c.threshold;ti.style.maxWidth="90px";
  const ab=el("button",null,"Applica soglia");
  ab.title="salva la nuova soglia e riclassifica subito";
  ab.onclick=e=>{e.stopPropagation();
    applyThreshold(parseFloat(ti.value));};
  thr.append(ti,ab);
  head.appendChild(thr);
  box.appendChild(head);

  const kinds=[...new Set(d.results.map(r=>r.kind))].sort();
  const bar=el("div","cfilters");
  const qi=el("input");qi.placeholder="filtra i risultati per parola chiave "+
    "(titolo, nome, percorso)…";qi.value=(state.cf||{}).q||"";
  qi.oninput=()=>{state.cf=state.cf||{};state.cf.q=qi.value.toLowerCase();
    drawConceptResults();};
  const ks=el("select");
  ks.appendChild(new Option("tutte le tipologie",""));
  for(const k of kinds)ks.appendChild(new Option(k,k));
  ks.value=(state.cf||{}).type||"";
  ks.onchange=()=>{state.cf=state.cf||{};state.cf.type=ks.value;
    drawConceptResults();};
  bar.append(qi,ks);
  const res=el("div");res.id="cresults";
  box.append(bar,res);
  drawConceptResults();
}

function drawConceptResults(){
  const res=document.getElementById("cresults");
  if(!res)return;
  const q=(state.cf||{}).q||"",ft=((state.cf||{}).type||"").toLowerCase();
  const rows=state.conceptData.results.filter(r=>
    (!ft||(r.kind+" "+(r.ext||"")).toLowerCase().includes(ft))&&
    (!q||(r.title+" "+r.path+" "+r.filename).toLowerCase().includes(q)));
  res.innerHTML="";
  res.appendChild(el("div","mini",rows.length+" di "+
    state.conceptData.results.length+" risultati"));
  if(!rows.length)res.appendChild(el("div","empty",
    "Nessun documento supera la soglia"+((q||ft)?" con questi filtri":"")+
    ": aggiungi parole o documenti campione, o abbassa la soglia."));
  for(const r of rows){
    const c=card(r);
    const acts=el("div","acts");
    acts.appendChild(el("span","mini","esempio:"));
    const ba=el("button","pos","A + positivo");
    ba.title="aggiungi all'ancoraggio positivo del concetto e riclassifica";
    ba.onclick=e=>{e.stopPropagation();
      addSampleDoc(state.conceptId,"pos",r.id).then(()=>loadList());};
    acts.appendChild(ba);
    const bb=el("button","neg","B − negativo");
    bb.title="aggiungi all'ancoraggio negativo del concetto e riclassifica";
    bb.onclick=e=>{e.stopPropagation();
      addSampleDoc(state.conceptId,"neg",r.id).then(()=>loadList());};
    acts.appendChild(bb);
    c.appendChild(acts);
    res.appendChild(c);
  }
}

/* ---- pagina categorie (home) ---- */
async function openCats(){
  state.view="cats";markSidebar();closeDetail();
  const box=$("#list");
  box.innerHTML='<div class="empty">Carico le categorie…</div>';
  try{
    const[cs,tp,gp,stats]=await Promise.all([
      api("/api/concepts"),api("/api/topics"),api("/api/groups"),
      api("/api/stats")]);
    box.innerHTML="";
    const head=el("div","card");head.style.cursor="default";
    head.appendChild(el("h3",null,"Categorizza il tuo archivio"));
    head.appendChild(el("div","meta",
      "Una categoria (concetto) raggruppa i documenti per significato: "+
      "ancoraggio A = parole/frasi, documenti campione (per percorso) e "+
      "tipi di file da avvicinare; B = quelli da allontanare. Con @Nome "+
      "combinii più categorie. Documenti in archivio: "+stats.docs+"."));
    const nb=el("button","btn","+ Nuova categoria");
    nb.style.width="auto";nb.style.marginTop="10px";
    nb.onclick=()=>{showConcepts();};
    head.appendChild(nb);
    box.appendChild(head);

    box.appendChild(el("h4","sec","Le tue categorie"));
    if(!cs.length){
      const e1=el("div","empty",
        "Non hai ancora categorie. Esempio: crea «famiglia» con parole "+
        "mamma, fratello, sorella, tipi .docx .pdf e come negativo @Honda.");
      box.appendChild(e1);}
    for(const c of cs){
      const cd=el("div","card");cd.style.cursor="default";
      cd.appendChild(el("h3",null,c.name+"  —  "+
        (c.members==null?"apri per calcolare":c.members+" documenti")));
      cd.appendChild(el("div","meta","A: "+
        (c.pos_text||"—").replace(/\n/g,"; ")+
        (c.pos_docs?"  + campioni":"")+
        (c.pos_exts?"  ["+c.pos_exts+(c.ext_mode==="solo"?" SOLO]":"]"):"")));
      if(c.neg_text||c.neg_docs||c.neg_exts)
        cd.appendChild(el("div","meta","B: "+
          (c.neg_text||"—").replace(/\n/g,"; ")+
          (c.neg_docs?"  + campioni":"")+
          (c.neg_exts?"  ["+c.neg_exts+"]":"")));
      const acts=el("div","acts");
      const v=el("button","pos","Vedi classificazione");
      v.onclick=e=>{e.stopPropagation();openConcept(c.id);};
      acts.appendChild(v);
      const ed=el("button",null,"Modifica");
      ed.onclick=e=>{e.stopPropagation();showConcepts();editConcept(c);};
      acts.appendChild(ed);
      const rm=el("button",null,"Elimina");
      rm.onclick=e=>{e.stopPropagation();
        removeConcept(c.id).then(()=>openCats());};
      acts.appendChild(rm);
      cd.appendChild(acts);
      box.appendChild(cd);
    }

    box.appendChild(el("h4","sec","Argomenti automatici (dal contenuto)"));
    const tc=el("div","chipline");
    if(!tp.length)tc.appendChild(el("span","mini",
      "— usa «Analisi semantica → Raggruppa» per crearli —"));
    for(const t of tp){
      const tag=el("span","tag",t.topic+" ("+t.n+")");
      tag.onclick=()=>{$("#q").value='topic:"'+t.topic+'"';onSearch();};
      tc.appendChild(tag);
    }
    box.appendChild(tc);

    box.appendChild(el("h4","sec","Categorie semantiche (gruppi ⚡)"));
    const gc=el("div","chipline");
    const semg=(gp||[]).filter(g=>g.query);
    if(!semg.length)gc.appendChild(el("span","mini",
      "— nessuna: sono gruppi smart con query sem:… —"));
    for(const g of semg){
      const tag=el("span","tag","⚡ "+g.name);
      tag.onclick=()=>{state.view="list";$("#sem").checked=true;
        $("#q").value=g.query.slice(4).trim();onSearch();};
      gc.appendChild(tag);
    }
    box.appendChild(gc);
  }catch(e){box.innerHTML="";toast("⚠ "+e.message);}
}

async function applyThreshold(v){
  if(isNaN(v)){toast("Soglia non valida");return;}
  const c=state.conceptData.concept;
  try{
    await post("/api/concepts/save",{id:c.id,name:c.name,
      pos_text:c.pos_text,pos_docs:c.pos_docs,pos_exts:c.pos_exts,
      neg_text:c.neg_text,neg_docs:c.neg_docs,neg_exts:c.neg_exts,
      ext_mode:c.ext_mode,threshold:v});
    toast("✔ soglia "+v.toFixed(2)+" salvata · riclassifico…");
    await loadList();
    await loadSidebar();
  }catch(e){toast("⚠ "+e.message,true);}
}

/* ---- duplicati ---- */
async function loadDupes(){
  const box=$("#list");box.innerHTML='<div class="empty">Calcolo…</div>';
  try{
    const groups=await api("/api/dupes");
    box.innerHTML="";
    if(!groups.length)box.appendChild(el("div","empty","Nessun duplicato 🎉"));
    for(const g of groups){
      const c=el("div","card");c.style.cursor="default";
      c.appendChild(el("h3",null,`${g.title} — ${g.n} copie`));
      for(const dd of g.docs){
        const p=el("div","path",`[${dd.id}] ${dd.path}`);
        p.style.cursor="pointer";p.onclick=()=>selectDoc(dd.id);
        c.appendChild(p);
      }
      box.appendChild(c);
    }
  }catch(e){box.innerHTML="";toast("⚠ "+e.message);}
}

/* ---- barra laterale ---- */
function markSidebar(){
  document.querySelectorAll("aside .item").forEach(i=>{
    const q=i.dataset.q||"";
    i.classList.toggle("sel",
      state.view==="dupes"?i.dataset.view==="dupes":
      state.view==="cats"?i.dataset.view==="cats":
      state.view==="concept"?i.dataset.concept==String(state.conceptId):
      q===state.q&&!i.dataset.view);
  });
}

async function loadSidebar(){
  try{
    const stats=await api("/api/stats");state.stats=stats;
    $("#n-all").textContent=stats.docs;
    $("#n-dupes").textContent=stats.dupes;
    const kd=$("#kinds");kd.innerHTML="";
    for(const k of stats.kinds.slice(0,12)){
      const i=el("div","item",k.kind);
      i.appendChild(el("span","n",k.n));
      i.onclick=()=>{$("#q").value="kind:"+k.kind;onSearch();};
      i.dataset.q="kind:"+k.kind;kd.appendChild(i);
    }
    state.groups=await api("/api/groups");
    const gb=$("#groups");gb.innerHTML="";
    if(!state.groups.length)gb.appendChild(el("div","mini","— nessuno —"));
    for(const g of state.groups){
      const i=el("div","item",(g.query?"⚡ ":"")+g.name);
      if(!g.query)i.appendChild(el("span","n",g.n));
      i.onclick=()=>{
        state.view="list";
        if(g.query&&g.query.startsWith("sem:")){
          $("#sem").checked=true;$("#q").value=g.query.slice(4).trim();
        }else{
          $("#q").value=g.query||("group:"+g.name);
        }
        onSearch();};
      gb.appendChild(i);
    }
    const topics=await api("/api/topics");
    const tb=$("#topics");tb.innerHTML="";
    if(!topics.length)
      tb.appendChild(el("div","mini","— usa «Analisi semantica» —"));
    for(const t of topics.slice(0,25)){
      const i=el("div","item",t.topic);
      i.appendChild(el("span","n",t.n));
      i.dataset.q='topic:"'+t.topic+'"';
      i.onclick=()=>{$("#q").value=i.dataset.q;onSearch();};
      tb.appendChild(i);
    }
    state.concepts=await api("/api/concepts");
    const cb=$("#concepts");cb.innerHTML="";
    if(!state.concepts.length)
      cb.appendChild(el("div","mini","— creali con «Concetti…» —"));
    for(const cpt of state.concepts){
      const i=el("div","item",cpt.name);
      if(cpt.members!=null)i.appendChild(el("span","n",cpt.members));
      i.dataset.concept=cpt.id;
      i.onclick=()=>openConcept(cpt.id);
      cb.appendChild(i);
    }
    const csel=$("#csel");
    const cur=csel.value;
    csel.innerHTML='<option value="">🏷️ concetto: tutti</option>';
    for(const cpt of state.concepts)
      csel.appendChild(new Option("🏷️ "+cpt.name+
        (cpt.members!=null?" ("+cpt.members+")":""),cpt.id));
    if(cur&&[...csel.options].some(o=>o.value===cur))csel.value=cur;
    const tg=$("#tags");tg.innerHTML="";
    if(!stats.tags.length)tg.appendChild(el("div","mini","— nessuno —"));
    for(const t of stats.tags){
      const i=el("div","item",t.tag);
      i.appendChild(el("span","n",t.n));
      i.onclick=()=>addQuery("tag:"+t.tag);i.dataset.q="tag:"+t.tag;
      tg.appendChild(i);
    }
    markSidebar();
  }catch(e){toast("⚠ "+e.message);}
}

/* ---- cartelle e indice ---- */
function showModal(){$("#modal").classList.add("on");loadSources();pollImport();}
function hideModal(){$("#modal").classList.remove("on");}

async function loadSources(){
  const box=$("#srclist");box.innerHTML='<div class="mini">…</div>';
  try{
    const[srcs,st]=await Promise.all([api("/api/sources"),
                                      api("/api/import/status")]);
    const running=st.running,root=((st.stats||{}).radice||st.path||"").toLowerCase();
    box.innerHTML="";
    if(!srcs.length){
      box.innerHTML='<div class="mini" style="padding:8px 0">Nessuna '+
        'cartella: aggiungi un percorso (es. D:\\Documenti).</div>';
      return;}
    for(const s of srcs){
      const row=el("div","srcrow");
      const p=el("div","p");
      p.appendChild(document.createTextNode(s.path));
      const busy=running&&root.startsWith(s.path.toLowerCase());
      const meta=el("small",null,
        `${s.docs} documenti · ultima scansione: `+
        (busy?"⏳ SCANSIONE IN CORSO…":
         (s.last_scan?s.last_scan.replace("T"," "):"mai"))+
        (s.exists?"":" · PERCORSO INESISTENTE"));
      p.appendChild(meta);row.appendChild(p);
      const up=el("button","upd",busy?"In corso…":"Aggiorna");
      if(busy)up.disabled=true;
      else up.onclick=()=>scanSource(s.id);
      row.appendChild(up);
      const rm=el("button",null,"Rimuovi");
      rm.title="toglie dalla lista; i documenti restano nell'indice";
      rm.onclick=()=>removeSource(s.id);row.appendChild(rm);
      box.appendChild(row);}
  }catch(e){
    box.innerHTML='<div class="mini" style="padding:8px 0">⚠ impossibile '+
      "caricare l'elenco delle cartelle: "+e.message+'</div>';}
}

async function addSource(){
  const p=$("#spath").value.trim();
  if(!p){toast("Scrivi un percorso");return;}
  try{await post("/api/sources/add",{path:p});
    $("#spath").value="";toast("Cartella aggiunta: "+p);
    await loadSources();}
  catch(e){toast("⚠ "+e.message);}
}

async function removeSource(id){
  try{await post("/api/sources/remove",{id});
    toast("Cartella rimossa dalla lista (i documenti restano)");
    await loadSources();}
  catch(e){toast("⚠ "+e.message);}
}

async function scanSource(id){
  try{await post("/api/sources/scan",{id});
    toast("Aggiornamento avviato");importOwned=true;pollImport();}
  catch(e){toast("⚠ "+e.message);}
}

async function scanAllSources(){
  try{await post("/api/sources/scan",{});
    toast("Aggiornamento di tutte le cartelle avviato");
    importOwned=true;pollImport();}
  catch(e){toast("⚠ "+e.message);}
}

let poller=null,importOwned=false;
function pollImport(){
  clearInterval(poller);
  poller=setInterval(async()=>{
    try{
      const st=await api("/api/import/status");
      if(!st.running&&(!st.stats||st.stats.fatto)){
        return;}
      const s=st.stats||{};
      if(st.running){
        $("#istatus").textContent=
          `scansione ${s.radice||st.path||""} · visti ${s.visti||0} · `+
          `nuovi ${s.nuovi||0} · aggiornati ${s.aggiornati||0} · `+
          `duplicati ${s.doppioni||0}`+
          (st.error?" · ERRORE: "+st.error:"");
        $("#ibar").style.width=(8+Math.min(80,(s.visti||0)%100))+"%";
        return;}
      clearInterval(poller);
      $("#ibar").style.width="100%";
      $("#istatus").textContent=
        `aggiornato: nuovi ${s.nuovi||0} · file modificati `+
        `${s.aggiornati||0} · duplicati ${s.doppioni||0}`;
      await loadSidebar();
      if(state.view==="list"&&!state.q)await loadList();
      if($("#modal").classList.contains("on"))await loadSources();
      if(importOwned){
        importOwned=false;
        toast(st.error?"⚠ Import interrotto: "+st.error
             :"✔ Indice aggiornato: "+(s.nuovi||0)+" nuovi, "+
              (s.aggiornati||0)+" modificati");}
    }catch(e){clearInterval(poller);}
  },1200);
}

/* ---- analisi semantica ---- */
function showAnalysis(){$("#amodal").classList.add("on");pollJobs();}
function hideA(){$("#amodal").classList.remove("on");}

async function doEmbed(){
  try{await post("/api/embed",{});toast("Calcolo embedding avviato");
    notifiedEmbed=false;pollJobs();}
  catch(e){toast("⚠ "+e.message);$("#anote").textContent=e.message;}
}
async function doCluster(){
  try{await post("/api/cluster",{k:parseInt($("#ak").value)||40});
    toast("Raggruppamento avviato");notifiedCluster=false;pollJobs();}
  catch(e){toast("⚠ "+e.message);}
}

let apoll=null,notifiedEmbed=true,notifiedCluster=true;
function pollJobs(){
  clearInterval(apoll);
  apoll=setInterval(async()=>{
    try{
      const[em,cl]=await Promise.all([api("/api/job/embed"),
                                      api("/api/job/cluster")]);
      $("#astate").textContent="Embedding in archivio: "+em.embeddings;
      const es=em.stats,cs=cl.stats,txt=[];
      let pct=0;
      if(em.running&&es){txt.push(`embedding ${es.done}/${es.total||"…"}`);
        pct=Math.min(50,(es.done/(es.total||1))*50);}
      if(cl.running&&cs){txt.push(`raggruppamento: ${cs.fase}`);
        pct=Math.max(pct,50+Math.min(50,cs.done/(cs.total||1)*50));}
      if(!em.running&&es&&es.fatto&&!notifiedEmbed){
        notifiedEmbed=true;
        if(es.errore)toast("⚠ embedding: "+es.errore);
        else{toast("✔ Embedding pronti: "+es.done+" documenti");
          $("#anote").textContent="Ora la ricerca semantica usa gli "+
            "embedding; puoi anche rilanciare il raggruppamento.";}}
      if(!cl.running&&cs&&cs.fatto&&!notifiedCluster){
        notifiedCluster=true;
        if(cs.errore)toast("⚠ raggruppamento: "+cs.errore);
        else if(cs.cluster)toast("✔ Creati "+cs.cluster+" argomenti");}
      if(es&&es.errore)txt.push("embedding: "+es.errore);
      if(cs&&cs.errore)txt.push("raggruppamento: "+cs.errore);
      $("#astatus").textContent=txt.join(" · ")||"pronto";
      if(!em.running&&!cl.running&&(es&&es.done||cs&&cs.cluster)){
        pct=100;await loadSidebar();}
      $("#abar").style.width=pct+"%";
      if(!em.running&&!cl.running)clearInterval(apoll);
    }catch(e){clearInterval(apoll);}
  },1500);
}

/* ---- concetti ---- */
function showConcepts(){newConceptForm();
  $("#cmodal").classList.add("on");loadConceptsList();}
function hideC(){$("#cmodal").classList.remove("on");}
function clearConceptForm(){
  ["cname","cpostext","cposdocs","cposexts","cnegtext","cnegdocs",
   "cnegexts"].forEach(id=>$("#"+id).value="");
  $("#cthr").value="0.10";$("#cstrict").checked=false;
  window._editingConcept=null;}
function newConceptForm(){clearConceptForm();
  if($("#cmodal").classList.contains("on"))$("#cname").focus();}

async function loadConceptsList(){
  const box=$("#clist");box.innerHTML='<div class="mini">…</div>';
  try{
    const cs=await api("/api/concepts");
    box.innerHTML="";
    if(!cs.length){box.innerHTML='<div class="mini" style="padding:6px 0">'+
      'Nessun concetto: creane uno qui sotto (es. «famiglia»).</div>';return;}
    for(const c of cs){
      const row=el("div","srcrow");
      const p=el("div","p");
      p.appendChild(document.createTextNode(c.name));
      p.appendChild(el("small",null,
        (c.members==null?"?":c.members)+" documenti classificati · soglia "+
        Number(c.threshold).toFixed(2)));
      row.appendChild(p);
      const open=el("button","upd","Vedi");open.onclick=()=>{
        hideC();openConcept(c.id);};
      row.appendChild(open);
      const ed=el("button",null,"Modifica");
      ed.onclick=()=>editConcept(c);row.appendChild(ed);
      const rm=el("button",null,"Elimina");
      rm.onclick=()=>removeConcept(c.id);row.appendChild(rm);
      box.appendChild(row);}
  }catch(e){box.innerHTML='<div class="mini">⚠ '+e.message+'</div>';}
}

function editConcept(c){
  $("#cname").value=c.name;$("#cthr").value=c.threshold;
  $("#cpostext").value=c.pos_text;$("#cposdocs").value=c.pos_docs;
  $("#cposexts").value=c.pos_exts;
  $("#cnegtext").value=c.neg_text;$("#cnegdocs").value=c.neg_docs;
  $("#cnegexts").value=c.neg_exts;
  $("#cstrict").checked=c.ext_mode==="solo";
  window._editingConcept=c.id;
}

async function saveConcept(){
  const name=$("#cname").value.trim();
  if(!name){toast("Dai un nome al concetto");return;}
  try{
    const id=await post("/api/concepts/save",{
      id:window._editingConcept||null,name:name,
      pos_text:$("#cpostext").value,pos_docs:$("#cposdocs").value,
      pos_exts:$("#cposexts").value,
      neg_text:$("#cnegtext").value,neg_docs:$("#cnegdocs").value,
      neg_exts:$("#cnegexts").value,
      ext_mode:$("#cstrict").checked?"solo":"bonus",
      threshold:parseFloat($("#cthr").value)||0.10});
    window._editingConcept=null;
    $("#cname").value="";$("#cpostext").value="";$("#cposdocs").value="";
    $("#cposexts").value="";$("#cnegtext").value="";$("#cnegdocs").value="";
    $("#cnegexts").value="";$("#cthr").value="0.10";
    $("#cstrict").checked=false;
    toast("✔ Categoria \""+name+"\" salvata");
    if(state.view==="concept"&&state.conceptId==id)await loadList();
    await loadConceptsList();await loadSidebar();
  }catch(e){toast("⚠ Salvataggio non riuscito: "+e.message,true);}
}

async function removeConcept(id){
  try{await post("/api/concepts/remove",{id});
    toast("Concetto eliminato");await loadConceptsList();await loadSidebar();}
  catch(e){toast("⚠ "+e.message);}
}

async function addSampleDoc(cid,pol,docId){
  try{await post("/api/concepts/add-doc",
      {id:cid,doc_id:docId||state.selId,polarity:pol});
    toast("Documento aggiunto come esempio "+
      (pol==="pos"?"positivo (A)":"negativo (B)"));
    await loadSidebar();}
  catch(e){toast("⚠ "+e.message);}
}

function quickConcept(docId,title){
  window._qdoc=docId;
  $("#qdoc").textContent="Documento: "+(title||docId);
  const sel=$("#qconcept");sel.innerHTML="";
  for(const cpt of (state.concepts||[]))
    sel.appendChild(new Option(cpt.name,cpt.id));
  $("#qmodal").classList.add("on");
}
function hideQ(){$("#qmodal").classList.remove("on");}
async function quickAdd(pol){
  if(!$("#qconcept").value){toast("Prima crea un concetto");return;}
  await addSampleDoc($("#qconcept").value,pol,window._qdoc);
  hideQ();
  if(state.view==="concept")loadList();
}

function openConcept(id){
  state.view="concept";state.conceptId=id;markSidebar();
  $("#csel").value=String(id);loadList();
}
$("#csel").addEventListener("change",()=>{
  const v=$("#csel").value;
  if(v){openConcept(parseInt(v,10));}
  else{state.view="list";markSidebar();
    $("#q").value="";onSearch();}
});

/* ---- util ---- */
function fmtSize(n){
  if(n<1024)return n+" B";
  const u=["KB","MB","GB"];let i=-1;
  do{n/=1024;i++;}while(n>=1024&&i<2);
  return n.toFixed(1)+" "+u[i];
}

$("#q").addEventListener("input",onSearch);
$("#sem").addEventListener("change",onSearch);
/* ---- indicatore permanente di scansione ---- */
let wasScanning=false;
async function scanWatch(){
  try{
    const st=await api("/api/import/status");
    const strip=$("#scanstrip");
    if(st.running){
      const s=st.stats||{};
      strip.innerHTML='<span class="dot"></span>Scansione in corso: '+
        (s.radice||st.path||"")+' · visti '+(s.visti||0)+' · nuovi '+
        (s.nuovi||0)+' · aggiornati '+(s.aggiornati||0);
      strip.classList.add("on");
      wasScanning=true;
    }else{
      strip.classList.remove("on");
      if(wasScanning){
        wasScanning=false;
        await loadSidebar();
        if(state.view==="list"&&!state.q)await loadList();
        const s=st.stats||{};
        toast("✔ Scansione completata: "+(s.nuovi||0)+" nuovi, "+
          (s.aggiornati||0)+" modificati");
      }
    }
  }catch(e){/* server occupato: riprova al prossimo giro */}
}
setInterval(scanWatch,3000);

/* ---- menu mobile ---- */
function toggleSidebar(force){
  const open=force!==undefined?force:!$("aside").classList.contains("open");
  $("aside").classList.toggle("open",open);
  $("#scrim").classList.toggle("on",open);
}
$("#burger").addEventListener("click",()=>toggleSidebar());
$("#scrim").addEventListener("click",()=>toggleSidebar(false));
document.addEventListener("click",e=>{
  if(e.target.closest("aside .item"))toggleSidebar(false);});
$("#q").addEventListener("keydown",e=>{if(e.key==="Escape")
  {$("#q").value="";onSearch();}});
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){closeDetail();hideModal();hideA();hideC();hideQ();}});
document.querySelectorAll("aside .item").forEach(i=>{
  i.onclick=()=>{
    if(i.dataset.view==="dupes"){state.view="dupes";markSidebar();loadDupes();}
    else if(i.dataset.view==="cats"){openCats();}
    else if(i.dataset.q!==undefined&&!i.dataset.view){
      state.view="list";$("#q").value=i.dataset.q||"";onSearch();}
  };
});
$("#spath").addEventListener("keydown",e=>{if(e.key==="Enter")addSource();});

loadSidebar();openCats();
</script></body></html>"""


def run(db_path, host: str = "127.0.0.1", port: int = 8300,
        open_browser: bool = True) -> None:
    Handler.db_path = db_path
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"devon web gui su http://{host}:{port}  (Ctrl+C per fermare)")
    if host in ("0.0.0.0", "::"):
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
            print(f"accessibile dalla rete locale: http://{lan_ip}:{port}")
        except OSError:
            pass
    if open_browser:
        url = f"http://127.0.0.1:{port}" if host in ("0.0.0.0", "::") \
            else f"http://{host}:{port}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # preriscaldo in background: carica la matrice dei vettori e il
    # modello così il primo clic utente non aspetta decine di secondi
    def warmup():
        time.sleep(1.5)
        try:
            conn = dbm.connect(db_path)
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
            if n:
                data = semanticm._load_embeddings_np(conn)
                if data is not None:
                    semanticm._doc_matrix(data)
                print(f"vettori in memoria: {n}")
        except Exception:
            pass
        try:
            if semanticm.embeddings_status()[0]:
                semanticm._get_model()
                print("modello di embedding pronto")
        except Exception:
            pass

    threading.Thread(target=warmup, args=(db_path,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto.")
