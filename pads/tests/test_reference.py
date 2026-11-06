import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.diagnostics import fingerprint
from runtime.reference import MANUALS, search, get_page


@pytest.fixture
def index(tmp_path):
    path = tmp_path / 'reference.sqlite'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE sources (id TEXT PRIMARY KEY,path TEXT,sha256 TEXT,pages INTEGER,cover TEXT)')
    db.execute('CREATE VIRTUAL TABLE pages USING fts5(manual UNINDEXED,page UNINDEXED,text)')
    for name in MANUALS:
        source = tmp_path / (name + '.pdf')
        source.write_bytes(b'test source fingerprint')
        db.execute('INSERT INTO sources VALUES (?,?,?,?,?)', (name, str(source), fingerprint(source), 1, 'test cover'))
        db.execute('INSERT INTO pages VALUES (?,?,?)', (name, 1, 'PutTrace connects native points.'))
    db.commit()
    db.close()
    return path


def test_search_and_page_keep_provenance(index):
    result = search(index, 'PutTrace', 2)
    assert len(result['matches']) == 2
    assert result['runtime_support'] == 'not_established_by_documentation'
    assert get_page(index, 'layout', 1)['source']['page_count'] == 1
    assert search(index, 'PutTrace OR nonexistent')['matches'] == []
    assert search(index, '" OR 1=1 --')['matches'] == []


def test_changed_source_refuses_old_index(index):
    (index.parent / 'layout.pdf').write_bytes(b'changed')
    with pytest.raises(ValueError, match='reference_source_changed'):
        search(index, 'PutTrace')


def test_missing_pages_refused(index):
    db = sqlite3.connect(index)
    db.execute("DELETE FROM pages WHERE manual='layout'")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match='reference_pages_incomplete'):
        get_page(index, 'designer', 1)


def test_invalid_queries_and_absent_index_do_not_create_files(tmp_path):
    path = tmp_path / 'absent.sqlite'
    for query, limit in [(' ', 1), ('x', True), ('x', 21)]:
        with pytest.raises(ValueError):
            search(path, query, limit)
    with pytest.raises(sqlite3.OperationalError):
        search(path, 'valid')
    assert not path.exists()


def test_invalid_pages(index):
    for manual, page in [('layout', True), ('layout', 0), ('unknown', 1), ('layout', 2)]:
        with pytest.raises(ValueError):
            get_page(index, manual, page)
