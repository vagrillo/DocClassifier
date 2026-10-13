"""Importazione ricorsiva di cartelle nell'indice.

Non sposta e non modifica nulla sul disco: legge i file, calcola l'hash
SHA-256 (per i duplicati), estrae il testo e scrive nel database. Se un
file è già indicizzato e non è cambiato (stessa data di modifica) viene
saltato; se è cambiato viene reindicizzato. I trigger sul database
mantengono l'indice full-text allineato.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import extract

# Directory ignorate durante la scansione (rumore tipico di una home)
DEFAULT_EXCLUDES = {
    "Library", ".Trash", ".cache", ".git", "node_modules",
    "__pycache__", ".venv", "venv", ".npm", ".docker",
}

# Su Windows si evitano inoltre le aree di sistema (sarebbero ore di
# scansione per file di sistema non documentali)
WINDOWS_EXCLUDES = {
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "AppData", "$Recycle.Bin", "$RECYCLE.BIN", "$Windows.~WS",
    "$Windows.~BT", "$WinREAgent", "Config.Msi", "Recovery",
    "System Volume Information", "OneDriveTemp", "WinSxS",
} | DEFAULT_EXCLUDES

_ON_WINDOWS = os.name == "nt"

MAX_FILE_BYTES = 250 * 1024 * 1024  # salta file enormi (video, dmg...)


def default_excludes() -> set[str]:
    return set(WINDOWS_EXCLUDES) if _ON_WINDOWS else set(DEFAULT_EXCLUDES)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_files(root: Path, excludes: set[str], include_hidden: bool):
    # su Windows i nomi di cartella non distinguono maiuscole/minuscole
    if _ON_WINDOWS:
        excl = {e.lower() for e in excludes}

        def excluded(name: str) -> bool:
            return name.lower() in excl
    else:
        excl = excludes

        def excluded(name: str) -> bool:
            return name in excl

    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        dirnames[:] = [d for d in dirnames if not excluded(d)]
        for name in filenames:
            if not include_hidden and name.startswith("."):
                continue
            yield Path(dirpath) / name


def _title_for(filename: str, text: str, pdf_title: str) -> str:
    if pdf_title.strip() and not pdf_title.startswith("(estrazione"):
        return pdf_title.strip()[:200]
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 3:
            return line[:200]
    return Path(filename).stem


def ingest_folder(
    conn: sqlite3.Connection,
    root: Path | list[Path] | None = None,
    excludes: set[str] | None = None,
    include_hidden: bool = False,
    verbose: bool = True,
    progress: dict | None = None,
    roots: list[Path] | None = None,
) -> dict[str, int]:
    """Indicizza ricorsivamente una o più radici (in sequenza).

    Ritorna un riepilogo dei conteggi. Se viene passato `progress`, il
    dizionario dei conteggi viene aggiornato in place durante la
    scansione (usato dalla web gui per mostrare l'avanzamento di
    import ancora in corso).
    """
    stats = progress if progress is not None else {}
    stats.update({"visti": 0, "nuovi": 0, "aggiornati": 0, "doppioni": 0,
                  "saltati": 0, "errori": 0, "fatto": False, "ultimo": "",
                  "radice": ""})
    excludes = default_excludes() if excludes is None else excludes
    if roots is None:
        roots = [root] if root is not None else []
    roots = [Path(r).expanduser() for r in roots]
    last_commit = time.time()

    for root in roots:
        stats["radice"] = str(root)
        for path in iter_files(root, excludes, include_hidden):
            try:
                size = path.stat().st_size
                if size > MAX_FILE_BYTES or size == 0:
                    stats["saltati"] += 1
                    continue
                mtime = dt.datetime.fromtimestamp(
                    path.stat().st_mtime).isoformat(timespec="seconds")
            except OSError:
                stats["errori"] += 1
                continue

            stats["visti"] += 1
            strpath = str(path)
            ext = path.suffix.lower()

            row = conn.execute(
                "SELECT id, mtime FROM documents WHERE path = ?",
                (strpath,)).fetchone()
            if row and row["mtime"] == mtime:
                continue  # già indicizzato e invariato

            try:
                digest = sha256_of(path)
            except OSError:
                # file bloccato o protetto dal sistema: solo metadati
                stats["errori"] += 1
                digest = ""

            if digest and conn.execute(
                    "SELECT 1 FROM documents WHERE sha256=? AND path!=? "
                    "LIMIT 1", (digest, strpath)).fetchone():
                stats["doppioni"] += 1

            text, author, pdf_title = extract.extract(path, ext)
            title = _title_for(path.name, text, pdf_title)
            # il nome file entra nel testo indicizzato: così i documenti
            # senza testo estraibile (formati non supportati) restano
            # ricercabili
            stored_text = (path.name + "\n" + text)[: extract.MAX_TEXT_CHARS]

            if row:
                conn.execute(
                    "UPDATE documents SET filename=?, ext=?, kind=?, size=?, "
                    "mtime=?, sha256=?, title=?, author=?, date=?, text=? "
                    "WHERE id=?",
                    (path.name, ext, extract.kind_for(ext), size, mtime,
                     digest, title, author, mtime[:10], stored_text, row["id"]))
                stats["aggiornati"] += 1
            else:
                conn.execute(
                    "INSERT INTO documents(path, filename, ext, kind, size, "
                    "mtime, added, sha256, title, author, date, text) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (strpath, path.name, ext, extract.kind_for(ext), size,
                     mtime, dt.datetime.now().isoformat(timespec="seconds"),
                     digest, title, author, mtime[:10], stored_text))
                stats["nuovi"] += 1
            stats["ultimo"] = strpath

            done = stats["nuovi"] + stats["aggiornati"]
            if done and done % 500 == 0:
                conn.commit()  # visibilità graduale per import enormi
            if time.time() - last_commit > 2.0:
                # commit frequente: libera presto il lock di scrittura,
                # così i salvataggi dalla web gui non restano in attesa
                conn.commit()
                last_commit = time.time()
            if stats["visti"] % 250 == 0:
                time.sleep(0.05)  # respiro: tiene reattiva la web gui
            if verbose and done and done % 100 == 0:
                print(f"  ... {stats['visti']} file visti, {done} indicizzati",
                      file=sys.stderr)

    conn.commit()
    stats["fatto"] = True
    return stats
