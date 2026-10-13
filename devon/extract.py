"""Estrazione del testo dai formati più comuni.

Tutto con la libreria standard; pypdf (PDF) e pytesseract (OCR) sono
opzionali: se assenti il documento viene comunque indicizzato con i soli
metadati. Nessuna funzione qui solleva eccezioni per formati sconosciuti
o file danneggiati: si accontentano di restituire testo parziale o vuoto.
"""

from __future__ import annotations

import email
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

# --- Mappa estensione -> "kind" (il tipo mostrato e filtrabile) -------

KIND_BY_EXT: dict[str, str] = {
    ".txt": "testo", ".md": "testo", ".rtf": "testo", ".csv": "testo",
    ".log": "testo", ".json": "testo", ".xml": "testo", ".yaml": "testo",
    ".yml": "testo", ".ini": "testo", ".py": "testo", ".sh": "testo",
    ".html": "pagina web", ".htm": "pagina web", ".webarchive": "pagina web",
    ".pdf": "PDF",
    ".doc": "Word", ".docx": "Word",
    ".xls": "Excel", ".xlsx": "Excel", ".ods": "Excel",
    ".ppt": "PowerPoint", ".pptx": "PowerPoint", ".odp": "PowerPoint",
    ".odt": "Word",
    ".epub": "e-book",
    ".eml": "email", ".emlx": "email",
    ".png": "immagine", ".jpg": "immagine", ".jpeg": "immagine",
    ".tif": "immagine", ".tiff": "immagine", ".gif": "immagine",
    ".heic": "immagine", ".bmp": "immagine",
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio",
    ".mp4": "video", ".mov": "video",
    ".zip": "archivio", ".dmg": "archivio", ".rar": "archivio",
}

TEXT_EXTS = {
    ".txt", ".md", ".rtf", ".csv", ".log", ".json", ".xml", ".yaml",
    ".yml", ".ini", ".py", ".sh", ".applescript", ".js", ".css", ".tsv",
}

MAX_TEXT_CHARS = 400_000  # tronca testi enormi per non gonfiare il db


def kind_for(ext: str) -> str:
    return KIND_BY_EXT.get(ext.lower(), "altro")


# --- HTML ---------------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def strip_html(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return "\n".join(p.parts)


# --- Singoli formati ----------------------------------------------------

def _read_text(path: Path) -> str:
    data = path.read_bytes()
    return data.decode("utf-8", errors="replace")


def _read_rtf(path: Path) -> str:
    raw = _read_text(path)
    # RTF: tiene i gruppi di testo, scarta i comandi {\...} e \...
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), raw)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", text)
    return text


def _read_zip_xml(path: Path, members: list[str]) -> str:
    """Estrae il testo dai member XML/HTML dentro uno zip (docx/odt/epub)."""
    out: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for member in members:
            if member in names:
                out.append(_xml_text(zf.read(member).decode("utf-8", "replace")))
                break
    return "\n".join(out)


def _xml_text(xml: str) -> str:
    # unisce il contenuto testuale di tutti gli elementi
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return re.sub(r"<[^>]+>", " ", xml)
    return " ".join(t for t in root.itertext() if t and t.strip())


def _read_docx(path: Path) -> tuple[str, str]:
    body = _read_zip_xml(path, ["word/document.xml"])
    author = ""
    with zipfile.ZipFile(path) as zf:
        if "docProps/core.xml" in zf.namelist():
            core = zf.read("docProps/core.xml").decode("utf-8", "replace")
            m = re.search(r"<dc:creator>(.*?)</dc:creator>", core, re.S)
            if m:
                author = m.group(1).strip()
    return body, author


def _read_odt(path: Path) -> tuple[str, str]:
    body = _read_zip_xml(path, ["content.xml"])
    author = ""
    with zipfile.ZipFile(path) as zf:
        if "meta.xml" in zf.namelist():
            meta = zf.read("meta.xml").decode("utf-8", "replace")
            m = re.search(r"<meta:creator>(.*?)</meta:creator>", meta, re.S)
            if m:
                author = m.group(1).strip()
    return body, author


def _read_xlsx(path: Path) -> str:
    """Testo di un foglio Excel: stringhe condivise + valori dei fogli."""
    out: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "xl/sharedStrings.xml" in names:
            out.append(_xml_text(
                zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")))
        for n in sorted(names):
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                out.append(_xml_text(zf.read(n).decode("utf-8", "replace")))
    return "\n".join(out)


def _read_pptx(path: Path) -> str:
    """Testo di una presentazione: le slide in ordine numerico."""
    out: list[str] = []

    def slide_no(name: str) -> int:
        m = re.search(r"slide(\d+)\.xml$", name)
        return int(m.group(1)) if m else 0

    with zipfile.ZipFile(path) as zf:
        slides = [n for n in zf.namelist()
                  if re.match(r"ppt/slides/slide\d+\.xml$", n)]
        for n in sorted(slides, key=slide_no):
            out.append(_xml_text(zf.read(n).decode("utf-8", "replace")))
    return "\n".join(out)


# --- Formati binari storici (.doc, .xls, .ppt) ---------------------------
# I vecchi formati Office sono contenitori binari OLE: senza librerie
# esterne si recuperano le sequenze di testo leggibili direttamente dai
# byte (le stringhe sono conservate in cp1252 o UTF-16). Estrazione
# euristica: pensata per rendere i file CERCABILI, non per ricostruire
# il documento.

_BIN_TEXT_MAX_BYTES = 8 * 1024 * 1024
_BIN_TEXT_MAX_CHARS = 60_000
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'’\-]*")


def _ok_word(w: str) -> bool:
    """Parola plausibile: scarta i token-spazzatura dei formati binari."""
    if len(w) < 2 or len(w) > 40:
        return False
    accented = sum(1 for c in w if "À" <= c <= "ÿ")
    return accented / len(w) < 0.5 or len(w) <= 12


def _keep_words(text: str) -> str:
    words = [m.group(0) for m in _WORD_RE.finditer(text)]
    return " ".join(w for w in words if _ok_word(w))


def _scrape_binary_text(path: Path) -> str:
    data = path.read_bytes()[:_BIN_TEXT_MAX_BYTES]
    runs: list[str] = []
    # sequenze UTF-16LE (caratteri ASCII+latini con byte alto nullo)
    for m in re.finditer(rb"(?:[\x20-\x7e\xa0-\xff]\x00){5,}", data):
        runs.append(m.group(0).decode("utf-16-le", "replace"))
    # sequenze cp1252/latine (byte stampabili consecutivi, min 6)
    for m in re.finditer(rb"[\x20-\x7e\xa0-\xff]{6,}", data):
        runs.append(m.group(0).decode("cp1252", "replace"))
    text = _keep_words("\n".join(runs))
    return text[:_BIN_TEXT_MAX_CHARS]


def _read_epub(path: Path) -> str:
    out: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            low = name.lower()
            if low.endswith((".xhtml", ".html", ".htm")):
                html = zf.read(name).decode("utf-8", "replace")
                out.append(strip_html(html))
    return "\n".join(out)


def _read_eml(path: Path) -> str:
    msg = email.message_from_binary_file(open(path, "rb"))
    header = "\n".join(
        f"{h}: {msg.get(h, '')}".strip()
        for h in ("From", "To", "Cc", "Subject", "Date")
        if msg.get(h)
    )
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(
                        part.get_content_charset() or "utf-8", "replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(
                msg.get_content_charset() or "utf-8", "replace"))
    return header + "\n\n" + "\n".join(parts)


def _read_pdf(path: Path) -> tuple[str, str, str]:
    """Ritorna (testo, autore, titolo); richiede pypdf (opzionale)."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return "", "", ""
    try:
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        meta = reader.metadata or {}
        author = str(meta.get("/Author", "") or "")
        title = str(meta.get("/Title", "") or "")
        return "\n".join(pages), author, title
    except Exception:
        return "", "", ""


def _read_image(path: Path) -> str:
    """OCR di immagini e PDF scannerizzati; richiede pytesseract."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return ""
    try:
        return pytesseract.image_to_string(Image.open(str(path)), lang="ita+eng")
    except Exception:
        return ""


# --- Punto d'ingresso ----------------------------------------------------

def extract(path: Path, ext: str) -> tuple[str, str, str]:
    """Estrae (testo, autore, titolo-extra) da un file. Mai eccezioni."""
    try:
        return _extract(path, ext)
    except Exception as exc:  # file illeggibile: indicizza comunque i metadati
        return "", "", f"(estrazione fallita: {exc})"


def _extract(path: Path, ext: str) -> tuple[str, str, str]:
    ext = ext.lower()
    if ext in TEXT_EXTS:
        return (_read_text(path) if ext != ".rtf" else _read_rtf(path)), "", ""
    if ext in (".html", ".htm"):
        return strip_html(_read_text(path)), "", ""
    if ext == ".docx":
        body, author = _read_docx(path)
        return body, author, ""
    if ext == ".odt" or ext == ".ods" or ext == ".odp":
        body, author = _read_odt(path)
        return body, author, ""
    if ext == ".xlsx":
        return _read_xlsx(path), "", ""
    if ext == ".pptx":
        return _read_pptx(path), "", ""
    if ext in (".doc", ".xls", ".ppt"):
        return _scrape_binary_text(path), "", ""
    if ext == ".epub":
        return _read_epub(path), "", ""
    if ext in (".eml", ".emlx"):
        return _read_eml(path), "", ""
    if ext == ".pdf":
        text, author, title = _read_pdf(path)
        if text.strip():
            return text, author, title
        # PDF senza testo estraibile: probabilmente scansionato -> OCR
        return _read_image(path), author, title
    if ext in KIND_BY_EXT and KIND_BY_EXT[ext] == "immagine":
        return _read_image(path), "", ""
    return "", "", ""  # formato non gestito: soli metadati
