"""L0 — turn an uploaded book into clean, chaptered text.

Four formats, one output shape: an ordered list of `{title, source_ref, text}`.
Everything downstream (chunking, embedding, extraction) only ever sees that shape, so
adding a format later is a parser, not a pipeline change.

The honest limits, in descending order of trustworthiness — this ranking is the whole
reason the list exists, because it decides how much to trust anything built on top:

- **Structured JSON/JSONL is exact.** Chapters arrive already separated, titled and
  ordered. Nothing is inferred. If you control the scraper, emit this: it is the only
  format with no failure mode of its own.
- **EPUB is reliable.** A zip of XHTML with a declared spine, so reading order and
  chapter boundaries are *stated*, not guessed. The realistic risk is scraper
  boilerplate (nav links, ads) landing inside the chapter body.
- **PDF is heuristic.** A PDF has pages, not chapters. Headings are detected from the
  text; when none are found the book is split into fixed page ranges rather than
  pretending. The report says which happened.
- **A scanned PDF yields nothing** — pypdf extracts no text from images. That is
  detected and reported as needing OCR (`minicpm-v` is the intended fallback), not
  passed downstream as an empty book.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import logs


@dataclass
class Chapter:
    title: str
    text: str
    source_ref: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class ParseResult:
    chapters: list[Chapter] = field(default_factory=list)
    title: str = ""
    author: str = ""
    warnings: list[str] = field(default_factory=list)
    method: str = ""          # how chapters were determined — honesty about heuristics

    @property
    def word_count(self) -> int:
        return sum(c.word_count for c in self.chapters)


# A heading line: "Chapter 12", "CHAPTER XIV", "12.", "Part Three", "Prologue".
# Anchored and length-capped so a sentence merely *containing* the word doesn't match.
_HEADING = re.compile(
    r"^\s*("
    r"(chapter|chap\.?|part|book|section|act)\s+([0-9]{1,3}|[ivxlcdm]{1,7}|[a-z\-]{3,12})"
    r"|prologue|epilogue|foreword|preface|introduction|afterword|interlude|appendix"
    r"|[0-9]{1,3}\s*[.—-]?\s*[A-Z][A-Za-z' ’-]{0,60}"
    r")\s*[:.—-]?\s*(.{0,60})?$",
    re.IGNORECASE,
)

# Chapters shorter than this are almost always a title page, a dedication or a
# section divider rather than real content; they get folded into the next chapter
# so the index isn't littered with two-word documents.
MIN_CHAPTER_WORDS = 120


def sniff_kind(filename: str, data: bytes) -> str:
    """Identify by content, not extension — a mislabelled upload should fail loudly
    at parse time, not silently produce an empty book."""
    if data[:4] == b"%PDF":
        return "pdf"
    head = data[:2048].lstrip()
    if head[:1] in (b"{", b"["):
        return "json"
    if data[:2] == b"PK":
        # EPUB is a zip whose first entry is a `mimetype` file. Check, because a
        # .docx and a .zip of text files are also PK.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
            if any(n == "mimetype" for n in names) or any(n.endswith(".opf") for n in names):
                return "epub"
        except zipfile.BadZipFile:
            return ""
        return ""
    lower = filename.lower()
    if lower.endswith(".txt") or lower.endswith(".md"):
        return "txt"
    # Last resort: if it decodes as text, treat it as text.
    try:
        data[:4096].decode("utf-8")
        return "txt"
    except UnicodeDecodeError:
        return ""


def _clean(text: str) -> str:
    """Normalise whitespace without destroying paragraph structure — paragraphs are
    the chunker's natural seam, so collapsing them all would cost retrieval quality."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ")
    # de-hyphenate words broken across a line end ("dun-\ngeon" -> "dungeon")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # a single newline inside a paragraph becomes a space; blank lines survive
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fold_short(chapters: list[Chapter]) -> list[Chapter]:
    """Merge sub-threshold fragments forward into the next real chapter."""
    out: list[Chapter] = []
    carry: Chapter | None = None
    for ch in chapters:
        if carry is not None:
            ch = Chapter(
                title=carry.title if carry.word_count >= ch.word_count else ch.title,
                text=(carry.text + "\n\n" + ch.text).strip(),
                source_ref=carry.source_ref or ch.source_ref,
            )
            carry = None
        if ch.word_count < MIN_CHAPTER_WORDS:
            carry = ch
            continue
        out.append(ch)
    if carry is not None:
        if out:
            out[-1] = Chapter(out[-1].title,
                              (out[-1].text + "\n\n" + carry.text).strip(),
                              out[-1].source_ref)
        elif carry.text.strip():
            out.append(carry)
    return out


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #

_OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "cnt": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def _epub_spine(zf: zipfile.ZipFile) -> tuple[list[str], str, str]:
    """Return `(hrefs in reading order, title, author)` from the OPF.

    An EPUB states its reading order in the spine, so this is reading a manifest,
    not guessing. `container.xml` is the one file whose location is fixed by spec.
    """
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//cnt:rootfile", _OPF_NS)
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB container.xml declares no rootfile")
    opf_path = rootfile.get("full-path")
    opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""

    opf = ET.fromstring(zf.read(opf_path))
    title_el = opf.find(".//dc:title", _OPF_NS)
    author_el = opf.find(".//dc:creator", _OPF_NS)
    title = (title_el.text or "").strip() if title_el is not None else ""
    author = (author_el.text or "").strip() if author_el is not None else ""

    # id -> href, then walk the spine so order comes from the book, not the zip.
    manifest: dict[str, str] = {}
    for item in opf.findall(".//opf:manifest/opf:item", _OPF_NS):
        item_id, href = item.get("id"), item.get("href")
        media = item.get("media-type", "")
        if item_id and href and ("html" in media or href.endswith((".xhtml", ".html", ".htm"))):
            manifest[item_id] = f"{opf_dir}/{href}" if opf_dir else href

    hrefs: list[str] = []
    for ref in opf.findall(".//opf:spine/opf:itemref", _OPF_NS):
        idref = ref.get("idref")
        if idref and idref in manifest:
            hrefs.append(manifest[idref])
    # A spine-less or broken EPUB still beats no book: fall back to manifest order.
    return (hrefs or list(manifest.values())), title, author


def parse_epub(path: Path) -> ParseResult:
    from bs4 import BeautifulSoup  # noqa: PLC0415 — heavy import, only when needed

    res = ParseResult(method="epub-spine")
    with zipfile.ZipFile(path) as zf:
        try:
            hrefs, res.title, res.author = _epub_spine(zf)
        except (KeyError, ValueError) as exc:
            res.warnings.append(f"could not read the EPUB spine ({exc}) — falling back to zip order")
            res.method = "epub-zip-order"
            hrefs = [n for n in zf.namelist() if n.endswith((".xhtml", ".html", ".htm"))]

        names = set(zf.namelist())
        for href in hrefs:
            # Zip entries are stored without a leading "./" and are case-sensitive.
            name = href if href in names else href.lstrip("./")
            if name not in names:
                res.warnings.append(f"spine references a missing file: {href}")
                continue
            try:
                # html.parser is stdlib — no lxml, no compiled dependency. It is
                # slower on a huge document but correct, and parsing happens once.
                soup = BeautifulSoup(zf.read(name), "html.parser")
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"could not parse {href}: {exc}")
                continue

            for tag in soup(["script", "style"]):
                tag.decompose()

            heading = soup.find(["h1", "h2", "h3"])
            title = heading.get_text(" ", strip=True) if heading else ""
            text = _clean(soup.get_text("\n"))
            if not text:
                continue
            res.chapters.append(Chapter(title=title, text=text, source_ref=href))

    res.chapters = _fold_short(res.chapters)
    for i, ch in enumerate(res.chapters, start=1):
        if not ch.title:
            ch.title = f"Chapter {i}"
    if not res.chapters:
        res.warnings.append("EPUB contained no readable documents")
    return res


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

PDF_PAGES_PER_BLOCK = 20   # fallback grouping when no headings are detectable


def parse_pdf(path: Path) -> ParseResult:
    from pypdf import PdfReader  # noqa: PLC0415

    res = ParseResult()
    reader = PdfReader(str(path))

    meta = reader.metadata or {}
    res.title = (meta.get("/Title") or "").strip()
    res.author = (meta.get("/Author") or "").strip()

    pages: list[str] = []
    for n, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 — one bad page must not lose the book
            res.warnings.append(f"page {n} failed to extract: {exc}")
            pages.append("")

    total_words = sum(len(p.split()) for p in pages)
    if total_words < 50 * max(1, len(pages)) // 10:
        # <5 words/page average: this is a scan, not a text PDF.
        res.warnings.append(
            f"only {total_words} words across {len(pages)} pages — this looks like a "
            "scanned PDF with no text layer. OCR is needed before it can be indexed."
        )

    # Pass 1: headings at the top of a page. A chapter almost always starts a page,
    # so restricting the search to the first few lines kills most false positives.
    starts: list[tuple[int, str]] = []
    for n, text in enumerate(pages):
        for line in [l.strip() for l in text.splitlines()[:4] if l.strip()][:2]:
            if len(line) <= 70 and _HEADING.match(line):
                starts.append((n, line))
                break

    if len(starts) >= 3:
        res.method = "pdf-headings"
        bounds = [s[0] for s in starts]
        if bounds[0] > 0:
            starts.insert(0, (0, "Front matter"))
            bounds.insert(0, 0)
        for i, (start_page, title) in enumerate(starts):
            end_page = bounds[i + 1] if i + 1 < len(bounds) else len(pages)
            text = _clean("\n".join(pages[start_page:end_page]))
            if text:
                res.chapters.append(Chapter(
                    title=title,
                    text=text,
                    source_ref=f"pages {start_page + 1}-{end_page}",
                ))
    else:
        res.method = "pdf-page-blocks"
        res.warnings.append(
            f"no chapter headings detected — split into {PDF_PAGES_PER_BLOCK}-page blocks. "
            "Citations still resolve to pages, but chapter titles are synthetic."
        )
        for start_page in range(0, len(pages), PDF_PAGES_PER_BLOCK):
            end_page = min(start_page + PDF_PAGES_PER_BLOCK, len(pages))
            text = _clean("\n".join(pages[start_page:end_page]))
            if text:
                res.chapters.append(Chapter(
                    title=f"Pages {start_page + 1}-{end_page}",
                    text=text,
                    source_ref=f"pages {start_page + 1}-{end_page}",
                ))

    res.chapters = _fold_short(res.chapters)
    if not res.chapters:
        res.warnings.append("PDF produced no extractable text")
    return res


# --------------------------------------------------------------------------- #
# plain text
# --------------------------------------------------------------------------- #

def parse_txt(path: Path) -> ParseResult:
    res = ParseResult(method="txt-headings")
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.replace("\r\n", "\n").split("\n")

    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s and len(s) <= 70 and _HEADING.match(s):
            starts.append((i, s))

    if len(starts) >= 2:
        if starts[0][0] > 0:
            starts.insert(0, (0, "Front matter"))
        for i, (start, title) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
            text = _clean("\n".join(lines[start + 1:end]))
            if text:
                res.chapters.append(Chapter(title=title, text=text,
                                            source_ref=f"lines {start + 1}-{end}"))
    else:
        res.method = "txt-whole"
        res.warnings.append("no chapter headings detected — indexed as a single document")
        text = _clean(raw)
        if text:
            res.chapters.append(Chapter(title=path.stem, text=text, source_ref="whole file"))

    res.chapters = _fold_short(res.chapters)
    return res


# --------------------------------------------------------------------------- #
# structured JSON / JSONL — the exact path
# --------------------------------------------------------------------------- #

def parse_json(path: Path) -> ParseResult:
    """Read chapters that are already separated, titled and ordered.

    Two accepted shapes, because a scraper naturally emits one or the other:

      JSONL  one chapter object per line
      JSON   {"title": ..., "author": ..., "chapters": [ {...}, ... ]}
             or a bare top-level list of chapter objects

    A chapter object needs `text` (aliases: content, body). Everything else is
    optional: `title` (aliases: name, chapter_title), `source_ref` (aliases: url,
    ref, source, link) and `position`/`index`/`chapter` to force ordering.

    This is the format to scrape into. Order comes from the file, titles come from
    the site, and the source URL survives all the way into the citation — which no
    other format can offer.
    """
    import json  # noqa: PLC0415

    res = ParseResult(method="json-structured")
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        res.warnings.append("the file is empty")
        return res

    records: list[Any] = []
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict):
            res.title = str(doc.get("title") or doc.get("book") or "").strip()
            res.author = str(doc.get("author") or doc.get("creator") or "").strip()
            records = doc.get("chapters") or doc.get("items") or []
            if not isinstance(records, list):
                raise ValueError("'chapters' is not a list")
        elif isinstance(doc, list):
            records = doc
        else:
            raise ValueError("top level is neither an object nor a list")
    except json.JSONDecodeError:
        # Not one JSON document — try JSONL, one object per line.
        res.method = "jsonl-structured"
        for n, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                res.warnings.append(f"line {n} is not valid JSON: {exc}")
        if not records:
            res.warnings.append("no valid JSON objects found — is this JSON or JSONL?")
            return res

    def pick(rec: dict[str, Any], *keys: str) -> str:
        for k in keys:
            v = rec.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    ordered: list[tuple[int, dict[str, Any]]] = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            res.warnings.append(f"entry {i + 1} is not an object — skipped")
            continue
        pos = rec.get("position", rec.get("index", rec.get("chapter")))
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            pos = i
        ordered.append((pos, rec))
    ordered.sort(key=lambda t: t[0])

    for n, (_, rec) in enumerate(ordered, start=1):
        text = _clean(pick(rec, "text", "content", "body"))
        if not text:
            res.warnings.append(f"entry {n} has no text — skipped")
            continue
        res.chapters.append(Chapter(
            title=pick(rec, "title", "name", "chapter_title") or f"Chapter {n}",
            text=text,
            # The URL is the best citation this project will ever get: it points at
            # the actual source, not at an offset inside a container file.
            source_ref=pick(rec, "source_ref", "url", "ref", "source", "link"),
        ))

    if not res.chapters:
        res.warnings.append("no chapters with text were found")
    return res


# --------------------------------------------------------------------------- #

PARSERS = {"epub": parse_epub, "pdf": parse_pdf, "txt": parse_txt, "json": parse_json}


def parse(path: Path, kind: str) -> ParseResult:
    fn = PARSERS.get(kind)
    if fn is None:
        raise ValueError(f"unsupported source kind: {kind or '(unknown)'}")
    with logs.timed("local", f"parse {kind}", level="info", file=path.name):
        res = fn(path)
    logs.info("local", f"parsed {len(res.chapters)} chapter(s), {res.word_count} words",
              method=res.method, warnings=len(res.warnings))
    return res


def report(res: ParseResult, book: dict[str, Any]) -> dict[str, Any]:
    """The human-readable parse report — written to `review/parse-report.json`.

    This is how L0 is proven alone: read the text, check the report.
    """
    return {
        "book": {"title": book.get("title", ""), "slug": book.get("slug", "")},
        "source": {"kind": book.get("source_kind", ""), "file": book.get("source_file", "")},
        "method": res.method,
        "chapters": len(res.chapters),
        "words": res.word_count,
        "warnings": res.warnings,
        "chapter_detail": [
            {"position": i, "title": c.title, "words": c.word_count, "source_ref": c.source_ref}
            for i, c in enumerate(res.chapters, start=1)
        ],
    }
