"""Interfaccia a riga di comando di devon.

Uso:  python3 -m devon [--db PERCORSO] COMANDO [opzioni]
Esempi:
    python3 -m devon import ~/Documents
    python3 -m devon search "verbale assemblea tag:personale"
    python3 -m devon similar 42
    python3 -m devon export --query "consiglio" --dest /Volumes/MacBook/Backup
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import db as dbm
from . import ingest as ingestm
from . import search as searchm
from . import similar as similarm


# --- utilità di stampa ----------------------------------------------------

def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1:.1f} {unit}"
        n /= 1024
    return f"{n} GB"


def print_doc_rows(rows, with_snippet: bool = False) -> None:
    if not rows:
        print("Nessun documento trovato.")
        return
    for r in rows:
        head = (f"[{r['id']}] {r['title']}  "
                f"({r['kind']}, {fmt_size(r['size'])}, {r['date']})")
        print(head)
        if with_snippet and r["snippet"]:
            print(f"     {r['snippet']}")
        print(f"     {r['path']}")


def rows_to_json(rows) -> str:
    return json.dumps(
        [{k: r[k] for k in r.keys() if k != "text"} for r in rows],
        ensure_ascii=False, indent=2)


# --- comandi ---------------------------------------------------------------

def cmd_init(args) -> None:
    conn = dbm.connect(args.db)
    path = dbm.default_db_path() if not args.db else Path(args.db)
    print(f"Database pronto: {path}" if not args.db
          else f"Database pronto: {Path(args.db)}")
    n = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    print(f"Documenti attualmente indicizzati: {n}")


def cmd_import(args) -> None:
    conn = dbm.connect(args.db)
    roots = []
    for raw in args.paths:
        root = Path(raw).expanduser()
        if not root.exists():
            print(f"!! percorso inesistente, salto: {root}", file=sys.stderr)
            continue
        roots.append(root)
        print(f"Indicizzo {root} ...")
    if not roots:
        return
    excludes = ingestm.default_excludes() | set(args.exclude or [])
    stats = ingestm.ingest_folder(
        conn, roots=roots, excludes=excludes, include_hidden=args.hidden)
    print(f"  visti {stats['visti']} | nuovi {stats['nuovi']} | "
          f"aggiornati {stats['aggiornati']} | copie duplicate "
          f"{stats['doppioni']} | saltati {stats['saltati']} | "
          f"errori {stats['errori']}")


def cmd_ls(args) -> None:
    conn = dbm.connect(args.db)
    where, params = [], []
    if args.kind:
        where.append("kind LIKE ? COLLATE NOCASE")
        params.append(f"%{args.kind}%")
    if args.tag:
        where.append("id IN (SELECT doc_id FROM tags WHERE tag = ? COLLATE NOCASE)")
        params.append(args.tag)
    if args.group:
        ids = searchm.group_subtree_ids(conn, args.group)
        if not ids:
            print(f"Gruppo inesistente: {args.group}")
            return
        ph = ",".join("?" * len(ids))
        where.append(f"id IN (SELECT doc_id FROM doc_groups WHERE group_id IN ({ph}))")
        params.extend(ids)
    sql = ("SELECT id, title, kind, size, date, path, '' AS snippet "
           "FROM documents")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    if args.json:
        print(rows_to_json(rows))
    else:
        print_doc_rows(rows)
        print(f"\n{len(rows)} documento(i) (max {args.limit})")


def cmd_search(args) -> None:
    conn = dbm.connect(args.db)
    query = " ".join(args.query)
    rows = searchm.search(conn, query, limit=args.limit)
    if args.json:
        print(rows_to_json(rows))
    else:
        print_doc_rows(rows, with_snippet=True)
        print(f"\n{len(rows)} risultato(i) per: {query}")


def cmd_show(args) -> None:
    conn = dbm.connect(args.db)
    d = conn.execute("SELECT * FROM documents WHERE id=?", (args.id,)).fetchone()
    if not d:
        print(f"Documento {args.id} inesistente.")
        sys.exit(1)
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (args.id,))]
    groups = [f"{r['name']}{' *' if r['query'] else ''}" for r in conn.execute(
        "SELECT g.name, g.query FROM groups g JOIN doc_groups dg "
        "ON dg.group_id=g.id WHERE dg.doc_id=? ORDER BY g.name", (args.id,))]
    exists = Path(d["path"]).exists()
    print(f"[{d['id']}] {d['title']}")
    print(f"  percorso : {d['path']}{'  (MANCANTE su disco!)' if not exists else ''}")
    print(f"  tipo     : {d['kind']} ({d['ext']}), {fmt_size(d['size'])}")
    print(f"  data     : {d['date']} | autore: {d['author'] or '—'}")
    print(f"  aggiunto : {d['added']} | sha256: {d['sha256'][:16]}…")
    print(f"  tag      : {', '.join(tags) if tags else '—'}")
    print(f"  gruppi   : {', '.join(groups) if groups else '—'}")
    if d["text"]:
        text = d["text"] if args.full else d["text"][:2000]
        print("\n--- testo " + ("(completo)" if args.full else "(primi 2000 caratteri)") + " ---")
        print(text)
    else:
        print("\n(nessun testo estraibile: solo metadati)")
    sims = similarm.similar_documents(conn, args.id, limit=5)
    if sims:
        print("\n--- vedi anche ---")
        for s in sims:
            print(f"  [{s['id']}] {s['score']:.2f}  {s['title']}")


def cmd_rm(args) -> None:
    conn = dbm.connect(args.db)
    for doc_id in args.ids:
        cur = conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        print(f"[{doc_id}] rimosso dall'indice"
              + ("" if cur.rowcount else " (inesistente)"))
    conn.commit()


def cmd_tag(args) -> None:
    conn = dbm.connect(args.db)
    if not conn.execute("SELECT 1 FROM documents WHERE id=?",
                        (args.id,)).fetchone():
        print(f"Documento {args.id} inesistente.")
        sys.exit(1)
    for spec in args.tags:
        if spec.startswith("-") and len(spec) > 1:
            conn.execute("DELETE FROM tags WHERE doc_id=? AND tag=?",
                         (args.id, spec[1:]))
        else:
            tag = spec.lstrip("+")
            if tag:
                conn.execute(
                    "INSERT OR IGNORE INTO tags(tag, doc_id) VALUES(?,?)",
                    (tag, args.id))
    conn.commit()
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (args.id,))]
    print(f"[{args.id}] tag: {', '.join(tags) if tags else '—'}")


def cmd_tags(args) -> None:
    conn = dbm.connect(args.db)
    rows = conn.execute(
        "SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag ORDER BY n DESC, tag"
    ).fetchall()
    if not rows:
        print("Nessun tag.")
        return
    width = max(len(r["tag"]) for r in rows)
    for r in rows:
        print(f"  {r['tag']:<{width}}  {r['n']} documento(i)")


def cmd_group(args) -> None:
    conn = dbm.connect(args.db)
    if args.action == "add":
        parent_id = None
        if args.parent:
            row = conn.execute("SELECT id FROM groups WHERE name=?",
                               (args.parent,)).fetchone()
            if not row:
                print(f"Gruppo padre inesistente: {args.parent}")
                sys.exit(1)
            parent_id = row["id"]
        conn.execute(
            "INSERT INTO groups(name, parent_id, query) VALUES(?,?,?)",
            (args.name, parent_id, args.query))
        conn.commit()
        tipo = "gruppo smart (query: " + args.query + ")" if args.query \
            else "gruppo"
        print(f"Creato {tipo}: {args.name}")
    elif args.action == "ls":
        rows = conn.execute(
            "SELECT g.id, g.name, g.query, p.name AS parent, "
            "(SELECT COUNT(*) FROM doc_groups dg WHERE dg.group_id=g.id) AS n "
            "FROM groups g LEFT JOIN groups p ON p.id=g.parent_id "
            "ORDER BY g.name").fetchall()
        if not rows:
            print("Nessun gruppo.")
            return
        for r in rows:
            info = f"  [{r['id']}] {r['name']}"
            if r["parent"]:
                info += f" (in {r['parent']})"
            if r["query"]:
                info += f"  SMART→ {r['query']}"
            else:
                info += f"  {r['n']} documento(i)"
            print(info)
    elif args.action == "rm":
        cur = conn.execute("DELETE FROM groups WHERE name=?", (args.name,))
        conn.commit()
        print("Gruppo eliminato." if cur.rowcount else "Gruppo inesistente.")
    elif args.action == "put":
        row = conn.execute("SELECT id, query FROM groups WHERE name=?",
                           (args.name,)).fetchone()
        if not row:
            print(f"Gruppo inesistente: {args.name} "
                  f"(crealo con: group add {args.name})")
            sys.exit(1)
        if row["query"]:
            print("È un gruppo smart: i membri sono calcolati dalla query, "
                  "non si aggiungono a mano.")
            sys.exit(1)
        for doc_id in args.ids:
            conn.execute(
                "INSERT OR IGNORE INTO doc_groups(doc_id, group_id) "
                "VALUES(?,?)", (doc_id, row["id"]))
        conn.commit()
        print(f"Aggiunti {len(args.ids)} documento(i) al gruppo {args.name}.")


def cmd_similar(args) -> None:
    from . import semantic
    conn = dbm.connect(args.db)
    sem = semantic.similar_semantic(conn, args.id, limit=args.limit)
    if sem is not None:
        used, items = sem
        print("Documenti simili"
              + (" (similarità semantica su embedding)" if used else ""))
        for s in items:
            print(f"[{s['id']}] {s['score']:.2f}  {s['title']}")
            print(f"     {s['filename']}")
        return
    sims = similarm.similar_documents(conn, args.id, limit=args.limit)
    if not sims:
        print("Nessun documento affine (servono almeno 2 documenti con testo).")
        return
    for s in sims:
        print(f"[{s['id']}] {s['score']:.2f}  {s['title']}")
        print(f"     {s['path']}")


def cmd_dupes(args) -> None:
    conn = dbm.connect(args.db)
    rows = conn.execute(
        "SELECT sha256, COUNT(*) AS n, MIN(title) AS title, "
        "GROUP_CONCAT(path, CHAR(10)) AS paths FROM documents "
        "GROUP BY sha256 HAVING n > 1 ORDER BY n DESC").fetchall()
    if not rows:
        print("Nessun duplicato in archivio.")
        return
    for r in rows:
        print(f"== {r['title']}  ({r['n']} copie)")
        for p in r["paths"].split("\n"):
            print(f"   {p}")
    print(f"\n{len(rows)} gruppo(i) di duplicati.")


def cmd_stats(args) -> None:
    conn = dbm.connect(args.db)
    tot = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS size FROM documents"
    ).fetchone()
    print(f"Documenti : {tot['n']}  ({fmt_size(tot['size'])})")
    withtext = conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE text != ''").fetchone()["n"]
    print(f"Con testo : {withtext}  ({tot['n'] - withtext} solo metadati)")
    print("\nPer tipo:")
    for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM documents "
            "GROUP BY kind ORDER BY n DESC LIMIT 15"):
        print(f"  {r['kind']:<12} {r['n']}")
    print("\nTag più usati:")
    for r in conn.execute(
            "SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag "
            "ORDER BY n DESC LIMIT 10"):
        print(f"  {r['tag']:<20} {r['n']}")
    dupes = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT sha256 FROM documents "
        "GROUP BY sha256 HAVING COUNT(*) > 1)").fetchone()["n"]
    print(f"\nGruppi di duplicati: {dupes}")


def cmd_check(args) -> None:
    conn = dbm.connect(args.db)
    missing = [r for r in conn.execute("SELECT id, path FROM documents")
               if not Path(r["path"]).exists()]
    if not missing:
        print("Tutti i file indicizzati sono presenti su disco.")
        return
    for r in missing:
        print(f"[{r['id']}] {r['path']}")
    print(f"\n{len(missing)} file indicizzati non trovati su disco "
          f"(rimuovili con: rm ID...)")


def cmd_export(args) -> None:
    conn = dbm.connect(args.db)
    ids: set[int] = set(args.ids or [])
    if args.query:
        rows = searchm.search(conn, args.query, limit=1_000_000)
        ids |= {r["id"] for r in rows}
    if args.tag:
        ids |= {r["doc_id"] for r in conn.execute(
            "SELECT doc_id FROM tags WHERE tag=? COLLATE NOCASE",
            (args.tag,))}
    if args.group:
        gids = searchm.group_subtree_ids(conn, args.group)
        if gids:
            ph = ",".join("?" * len(gids))
            ids |= {r["doc_id"] for r in conn.execute(
                f"SELECT doc_id FROM doc_groups WHERE group_id IN ({ph})",
                gids)}
    if not ids:
        print("Nessun documento selezionato (usa --query, --tag, --group "
              "oppure elenca degli ID).")
        sys.exit(1)

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    copied = missing = 0
    total = 0
    for doc_id in sorted(ids):
        d = conn.execute("SELECT path, filename FROM documents WHERE id=?",
                         (doc_id,)).fetchone()
        src = Path(d["path"])
        if not src.exists():
            print(f"!! mancante su disco: [{doc_id}] {src}", file=sys.stderr)
            missing += 1
            continue
        target = dest / d["filename"]
        stem, suffix = target.stem, target.suffix
        n = 1
        while target.exists():
            n += 1
            target = dest / f"{stem}__{n}{suffix}"
        shutil.copy2(src, target)
        copied += 1
        total += src.stat().st_size
    print(f"Esportati {copied} documenti ({fmt_size(total)}) in {dest}")
    if missing:
        print(f"Attenzione: {missing} file non più presenti su disco.")


def cmd_semsearch(args) -> None:
    from . import semantic
    conn = dbm.connect(args.db)
    query = " ".join(args.query)
    results = semantic.semantic_search(conn, query, limit=args.limit)
    if not results:
        print("Nessun documento affine alla query.")
        return
    for r in results:
        print(f"[{r['id']}] {r['score']:.2f}  {r['title']}")
        print(f"     {r['path']}")
    note = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    if not note:
        print("\n(fallback lessicale TF-IDF: per la semantica completa "
              "installa fastembed e lancia `python3 -m devon embed`)")


def cmd_embed(args) -> None:
    from . import semantic
    conn = dbm.connect(args.db)
    ok, msg = semantic.embeddings_status()
    if not ok:
        print(msg)
        sys.exit(1)
    print(f"Calcolo embedding ({semantic.MODEL_NAME}) in corso…")
    stats = semantic.embed_all(conn)
    if stats["errore"]:
        print("Errore:", stats["errore"])
        sys.exit(1)
    print(f"Embedding calcolati: {stats['done']} su {stats['total']} documenti.")


def cmd_cluster(args) -> None:
    from . import semantic
    conn = dbm.connect(args.db)
    print(f"Raggruppamento per contenuti (k={args.k})…")
    stats = semantic.cluster_documents(
        conn, k=args.k,
        kinds=None if args.all_kinds else semantic.DOC_KINDS)
    if stats["errore"]:
        print("Errore:", stats["errore"])
        sys.exit(1)
    print(f"Creati {stats['cluster']} argomenti (colonna `topic`). "
          f"Vedi `python3 -m devon topics`.")


def cmd_topics(args) -> None:
    conn = dbm.connect(args.db)
    rows = conn.execute(
        "SELECT topic, COUNT(*) AS n FROM documents WHERE topic != '' "
        "GROUP BY topic ORDER BY n DESC").fetchall()
    if not rows:
        print("Nessun argomento: lancia `python3 -m devon cluster`.")
        return
    for r in rows:
        print(f"  {r['n']:6}  {r['topic']}")


def cmd_classify(args) -> None:
    from . import semantic
    conn = dbm.connect(args.db)
    if args.all:
        rows = conn.execute(
            "SELECT id FROM documents WHERE id NOT IN (SELECT doc_id FROM "
            "doc_groups) AND text != '' ORDER BY id").fetchall()
        ids = [r["id"] for r in rows]
    else:
        ids = [args.id]
    applied = 0
    for doc_id in ids:
        sugg = semantic.suggest_groups(conn, doc_id, limit=1)
        if not sugg:
            continue
        s = sugg[0]
        print(f"[{doc_id}] → {s['name']} (affinità {s['score']:.2f})")
        if args.apply and s["score"] >= args.min_score:
            g = conn.execute("SELECT id FROM groups WHERE name=?",
                             (s["name"],)).fetchone()
            conn.execute("INSERT OR IGNORE INTO doc_groups(doc_id, group_id) "
                         "VALUES(?,?)", (doc_id, g["id"]))
            applied += 1
    if args.apply:
        conn.commit()
        print(f"\nAssegnati {applied} documenti ai gruppi suggeriti "
              f"(soglia {args.min_score}).")


def cmd_serve(args) -> None:
    from . import webapp
    webapp.run(args.db, host=args.host, port=args.port,
               open_browser=not args.no_browser)


# --- parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="devon",
        description="Gestore documentale personale in puro Python "
                    "(ispirato a DEVONthink). Il database predefinito è "
                    "~/.devon/library.db.")
    p.add_argument("--db", help="percorso del database (default ~/.devon/library.db)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMANDO")

    s = sub.add_parser("init", help="crea/apre il database")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("import", help="indicizza una cartella (o un file)",
                       aliases=["importa"])
    s.add_argument("paths", nargs="+", metavar="PERCORSO")
    s.add_argument("--hidden", action="store_true",
                   help="includi file e cartelle nascoste")
    s.add_argument("--exclude", action="append", metavar="NOME",
                   help="nome di cartella da ignorare (ripetibile)")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("ls", help="elenca documenti (filtri opzionali)")
    s.add_argument("--kind", help="filtra per tipo (pdf, testo, email...)")
    s.add_argument("--tag", help="filtra per tag")
    s.add_argument("--group", help="filtra per gruppo")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("search", help="ricerca full-text con snippet",
                       aliases=["cerca"])
    s.add_argument("query", nargs="+", help='es: "verbale assemblea" '
                                            'tag:personale kind:pdf -bozza')
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("show", help="dettagli di un documento")
    s.add_argument("id", type=int)
    s.add_argument("--full", action="store_true", help="tutto il testo")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("rm", help="rimuove documenti dall'INDICE "
                                  "(non tocca i file)")
    s.add_argument("ids", nargs="+", type=int)
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("tag", help="aggiunge/rimuove tag: tag 12 +personale -bozza")
    s.add_argument("id", type=int)
    s.add_argument("tags", nargs="+", metavar="+tag|-tag")
    s.set_defaults(func=cmd_tag)

    s = sub.add_parser("tags", help="elenca i tag in uso")
    s.set_defaults(func=cmd_tags)

    s = sub.add_parser("group", help="gestione gruppi (normali e smart)")
    gsub = s.add_subparsers(dest="action", required=True, metavar="AZIONE")
    g = gsub.add_parser("add", help="crea un gruppo")
    g.add_argument("name")
    g.add_argument("--parent", help="gruppo padre (sottogruppo)")
    g.add_argument("--query", help="query: crea un gruppo SMART "
                                   "(membri calcolati automaticamente)")
    g.set_defaults(func=lambda a: cmd_group_replace(a, "add"))
    g = gsub.add_parser("ls", help="elenca i gruppi")
    g.set_defaults(func=lambda a: cmd_group_replace(a, "ls"))
    g = gsub.add_parser("rm", help="elimina un gruppo")
    g.add_argument("name")
    g.set_defaults(func=lambda a: cmd_group_replace(a, "rm"))
    g = gsub.add_parser("put", help="mette documenti in un gruppo")
    g.add_argument("name")
    g.add_argument("ids", nargs="+", type=int)
    g.set_defaults(func=lambda a: cmd_group_replace(a, "put"))

    s = sub.add_parser("similar", help="documenti affini (vedi anche)")
    s.add_argument("id", type=int)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_similar)

    s = sub.add_parser("semsearch",
                       help="ricerca semantica per significato")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_semsearch)

    s = sub.add_parser("embed", help="calcola gli embedding neurali "
                                     "(richiede: pip3 install fastembed)")
    s.set_defaults(func=cmd_embed)

    s = sub.add_parser("cluster", help="raggruppa i documenti per "
                                       "contenuto in argomenti (topic)")
    s.add_argument("-k", type=int, default=40,
                   help="numero di argomenti da creare (default 40)")
    s.add_argument("--all-kinds", action="store_true",
                   help="includi anche i file non documento "
                        "(codice, licenze...)")
    s.set_defaults(func=cmd_cluster)

    s = sub.add_parser("topics", help="elenca gli argomenti trovati")
    s.set_defaults(func=cmd_topics)

    s = sub.add_parser("classify", help="suggerisce/assegna il gruppo di "
                                        "appartenenza per contenuto")
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--all", action="store_true",
                   help="elabora tutti i documenti senza gruppo")
    s.add_argument("--apply", action="store_true",
                   help="assegna davvero il gruppo suggerito")
    s.add_argument("--min-score", type=float, default=0.15)
    s.set_defaults(func=cmd_classify)

    s = sub.add_parser("dupes", help="elenca i duplicati (stesso contenuto)")
    s.set_defaults(func=cmd_dupes)

    s = sub.add_parser("stats", help="statistiche dell'archivio")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("check", help="verifica i file mancanti su disco")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("export", help="copia documenti in una cartella "
                                      "(es. disco esterno)")
    s.add_argument("--query", help="esporta i risultati di questa ricerca")
    s.add_argument("--tag", help="esporta i documenti con questo tag")
    s.add_argument("--group", help="esporta i documenti di questo gruppo")
    s.add_argument("ids", nargs="*", type=int, help="ID specifici")
    s.add_argument("--dest", required=True, metavar="CARTELLA")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("serve", help="interfaccia grafica web locale")
    s.add_argument("--port", type=int, default=8300)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-browser", action="store_true",
                   help="non aprire automaticamente il browser")
    s.set_defaults(func=cmd_serve)

    return p


def cmd_group_replace(args, action: str) -> None:
    args.action = action
    cmd_group(args)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:
        pass
    return 0
