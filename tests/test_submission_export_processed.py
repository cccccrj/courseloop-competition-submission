"""Processed export preserves content and excludes private source records."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("processed_export", Path(__file__).parents[1] / "scripts/submission_export_processed.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    data = root / "data/processed"
    data.mkdir(parents=True)
    with sqlite3.connect(data / "courseloop.db") as conn:
        for name in module.CONTENT_TABLES:
            conn.execute('CREATE TABLE "' + name + '"(id INTEGER PRIMARY KEY, content TEXT)')
            conn.execute('INSERT INTO "' + name + '" VALUES(7,?)', ("课程内容",))
        for name in ("users", "practice_attempts", "app_settings"):
            conn.execute('CREATE TABLE "' + name + '"(value TEXT)')
            conn.execute('INSERT INTO "' + name + '" VALUES(?)', ("private-fixture-record",))
    with sqlite3.connect(data / "rag_fts.db") as conn:
        conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,course UNINDEXED,doc_type UNINDEXED,source UNINDEXED,title_path UNINDEXED,page_start UNINDEXED,page_end UNINDEXED,chunk_index UNINDEXED,tokenize='trigram')")
        conn.execute("INSERT INTO chunks(rowid,text,course,doc_type,source,title_path,page_start,page_end,chunk_index) VALUES(9,'课程内容','os','lecture','课程','章节',1,2,0)")
        conn.execute("CREATE TABLE build_meta(key TEXT,value TEXT)")
        conn.execute("INSERT INTO build_meta VALUES('host','private-fixture-record')")
    return root


def test_content_only_and_empty_personal_schema(source, tmp_path):
    output = tmp_path / "out"
    info = module.export(source, output)
    assert info["content_tables"] == {t: 1 for t in module.CONTENT_TABLES}
    with sqlite3.connect(output / "courseloop.db") as conn:
        for name in ("users", "practice_attempts", "app_settings"):
            assert conn.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0] == 0
        assert conn.execute("SELECT id,content FROM problems").fetchone() == (7, "课程内容")
    assert all(b"private-fixture-record" not in p.read_bytes() for p in output.glob("*.db"))


def test_fts_ids_locations_hashes_and_no_host_meta(source, tmp_path):
    output = tmp_path / "out"
    module.export(source, output)
    with sqlite3.connect(output / "rag_fts.db") as conn:
        assert conn.execute("SELECT rowid,course,page_start,page_end FROM chunks").fetchone() == (9, "os", 1, 2)
        assert conn.execute("SELECT count(*) FROM chunk_hashes").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM build_meta").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chunks WHERE chunks MATCH '课程内'").fetchone()[0] == 1


def test_source_bytes_unchanged(source, tmp_path):
    before = {p: p.read_bytes() for p in (source / "data/processed").glob("*.db")}
    module.export(source, tmp_path / "out")
    assert all(path.read_bytes() == content for path, content in before.items())


def test_existing_destination_rejected(source, tmp_path):
    with pytest.raises(ValueError, match="Destination exists"):
        module.export(source, tmp_path)


def test_missing_content_table_fails_before_output(source, tmp_path):
    with sqlite3.connect(source / "data/processed/courseloop.db") as conn:
        conn.execute("DROP TABLE problems")
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="Missing content table"):
        module.export(source, output)
    assert not output.exists()


def test_runtime_fts_rebuild_preserves_content_and_ids(source, tmp_path):
    from scripts.submission_provision_processed import rebuild_only
    output = tmp_path / "out"
    module.export(source, output)
    result = rebuild_only(output)
    assert result["content_unchanged"] is True
    with sqlite3.connect(output / "rag_fts.db") as conn:
        assert conn.execute("SELECT rowid,text FROM chunks").fetchone() == (9, "课程内容")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_runtime_fts_rebuild_rejects_changed_input(source, tmp_path):
    from scripts.submission_provision_processed import rebuild_only
    output = tmp_path / "out"
    module.export(source, output)
    with sqlite3.connect(output / "rag_fts.db") as conn:
        conn.execute("INSERT INTO build_meta VALUES('changed','fixture')")
    with pytest.raises(ValueError, match="Export hash mismatch"):
        rebuild_only(output)
