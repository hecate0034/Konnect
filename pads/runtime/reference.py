"""Local PDF index. Manuals are reference data, never executable instructions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .diagnostics import fingerprint

MANUALS = {
    "designer": "PADS Designer Automation Reference.pdf",
    "common": "Common Automation Reference.pdf",
    "layout": "Layout Automation Reference.pdf",
}


def build_index(manual_dir: Path, output: Path):
    from pypdf import PdfReader
    # Exclusive creation: an existing validated index is never overwritten in place.
    with output.open("xb"):
        pass
    try:
        with sqlite3.connect(output) as db:
            db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, path TEXT, sha256 TEXT, pages INTEGER, cover TEXT)")
            db.execute("CREATE VIRTUAL TABLE pages USING fts5(manual UNINDEXED, page UNINDEXED, text)")
            for name, filename in MANUALS.items():
                path = (manual_dir / filename).resolve(strict=True)
                before = fingerprint(path)
                reader = PdfReader(path)
                cover = reader.pages[0].extract_text()
                db.execute("INSERT INTO sources VALUES (?,?,?,?,?)", (name, str(path), before, len(reader.pages), cover))
                for number, page in enumerate(reader.pages, 1):
                    db.execute("INSERT INTO pages VALUES (?,?,?)", (name, number, page.extract_text()))
                if before != fingerprint(path):
                    raise ValueError(f"source_changed_during_index: {name}")
    except BaseException:
        # Keep a failed index visibly unusable; no caller can query absent sources.
        raise
    finally:
        # sqlite context managers commit/rollback but do not close the connection.
        if 'db' in locals():
            db.close()
    return {"status": "indexed", "sources": len(MANUALS)}


def open_verified(index: Path):
    db = sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT id,path,sha256,pages,cover FROM sources").fetchall()
        if {r[0] for r in rows} != set(MANUALS):
            raise ValueError("reference_index_incomplete")
        sources = {}
        for name, path, sha, pages, cover in rows:
            if fingerprint(Path(path)) != sha:
                raise ValueError(f"reference_source_changed: {name}")
            count, unique, first, last = db.execute(
                "SELECT count(*),count(DISTINCT page),min(CAST(page AS INTEGER)),max(CAST(page AS INTEGER)) FROM pages WHERE manual=?",
                (name,),
            ).fetchone()
            if (count, unique, first, last) != (pages, pages, 1, pages):
                raise ValueError(f"reference_pages_incomplete: {name}")
            sources[name] = {"path": path, "sha256": sha, "page_count": pages, "cover": cover}
        return db, sources
    except BaseException:
        db.close()
        raise


def search(index: Path, query: str, limit: int = 8):
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValueError("query must contain 1..300 characters")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer in 1..20")
    # Literal words only; callers cannot inject FTS operators or SQL.
    expression = " AND ".join('"' + w.replace('"', '""') + '"' for w in query.split())
    db, sources = open_verified(index)
    try:
        rows = db.execute("SELECT manual,page,snippet(pages,2,'[',']','…',45) FROM pages WHERE pages MATCH ? ORDER BY rank LIMIT ?", (expression, limit)).fetchall()
        return {"matches": [{"manual": m, "pdf_page": int(p), "excerpt": t, "source": sources[m]} for m, p, t in rows],
                "source_kind": "vendor_manual", "runtime_support": "not_established_by_documentation"}
    finally:
        db.close()


def get_page(index: Path, manual: str, page: int):
    if manual not in MANUALS or type(page) is not int or page < 1:
        raise ValueError("invalid manual or PDF page")
    db, sources = open_verified(index)
    try:
        row = db.execute("SELECT text FROM pages WHERE manual=? AND page=?", (manual, page)).fetchone()
        if row is None:
            raise ValueError("page_out_of_range")
        return {"manual": manual, "pdf_page": page, "text": row[0], "source": sources[manual],
                "source_kind": "vendor_manual", "runtime_support": "not_established_by_documentation"}
    finally:
        db.close()
