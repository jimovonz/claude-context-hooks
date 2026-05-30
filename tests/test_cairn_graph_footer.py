"""Tests for cairn_graph_footer module."""

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
from lib.cairn_graph_footer import (
    _extract_source_file, _extract_line_range, _find_graph_db,
    _resolve_functions, _query_cairn, _format_footer,
    _format_memory, generate_footer, _load_seen, _SESSION_SEEN_FILE,
)

GRAPH_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    confidence REAL DEFAULT 1.0,
    confidence_tier TEXT DEFAULT 'EXTRACTED'
);
"""

CAIRN_SCHEMA = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    topic TEXT,
    content TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    associated_files TEXT DEFAULT '[]',
    updated_at TEXT,
    deleted_at TEXT
);
"""


@pytest.fixture
def graph_repo(tmp_path):
    """Create a temp repo with graph.db and source files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("def foo():\n    pass\n" * 10)

    gdir = tmp_path / ".code-review-graph"
    gdir.mkdir()
    db = gdir / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(GRAPH_SCHEMA)
    fp = str(src / "main.py")
    conn.execute(
        "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, updated_at) "
        "VALUES ('Function', 'foo', ?, ?, 1, 20, ?)",
        (f"{fp}::foo", fp, time.time())
    )
    conn.execute(
        "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, is_test, updated_at) "
        "VALUES ('Test', 'test_foo', ?, ?, 1, 10, 1, ?)",
        (f"{tmp_path}/tests/test_main.py::test_foo", f"{tmp_path}/tests/test_main.py", time.time())
    )
    conn.execute(
        "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line) "
        "VALUES ('CALLS', ?, ?, ?, 5)",
        (f"{fp}::bar", f"{fp}::foo", fp)
    )
    conn.execute(
        "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line) "
        "VALUES ('CALLS', ?, ?, ?, 15)",
        (f"{tmp_path}/other.py::baz", f"{fp}::foo", f"{tmp_path}/other.py")
    )
    conn.execute(
        "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line) "
        "VALUES ('TESTED_BY', ?, ?, ?, 0)",
        (f"{fp}::foo", f"{tmp_path}/tests/test_main.py::test_foo", fp)
    )
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture
def cairn_db(tmp_path):
    """Create a temp cairn.db with test memories."""
    db_path = tmp_path / "cairn.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(CAIRN_SCHEMA)
    conn.execute(
        "INSERT INTO memories (type, topic, content, confidence, associated_files, updated_at) "
        "VALUES ('correction', 'test-fix', 'Fixed the broken import path', 0.9, ?, '2026-05-02 10:00:00')",
        (f'["{tmp_path}/src/main.py"]',)
    )
    conn.execute(
        "INSERT INTO memories (type, topic, content, confidence, associated_files, updated_at) "
        "VALUES ('decision', 'architecture', 'Use factory pattern', 0.8, ?, '2026-05-01 10:00:00')",
        (f'["{tmp_path}/src/main.py"]',)
    )
    conn.execute(
        "INSERT INTO memories (type, topic, content, confidence, associated_files, updated_at) "
        "VALUES ('correction', 'low-conf', 'Maybe wrong', 0.4, ?, '2026-05-02 12:00:00')",
        (f'["{tmp_path}/src/main.py"]',)
    )
    conn.execute(
        "INSERT INTO memories (type, topic, content, confidence, associated_files, updated_at, deleted_at) "
        "VALUES ('correction', 'deleted', 'Old fix', 0.9, ?, '2026-04-30 10:00:00', '2026-05-01 10:00:00')",
        (f'["{tmp_path}/src/main.py"]',)
    )
    conn.execute(
        "INSERT INTO memories (type, topic, content, confidence, associated_files, updated_at) "
        "VALUES ('fact', 'just-a-fact', 'Some info', 0.9, ?, '2026-05-02 11:00:00')",
        (f'["{tmp_path}/src/main.py"]',)
    )
    conn.commit()
    conn.close()
    return db_path


# --- Command detection ---

class TestExtractSourceFile:
    def test_cat_python(self, tmp_path):
        (tmp_path / "foo.py").touch()
        result = _extract_source_file("cat foo.py", str(tmp_path))
        assert result == str(tmp_path / "foo.py")

    def test_cat_with_flags(self, tmp_path):
        (tmp_path / "foo.py").touch()
        result = _extract_source_file("cat -n foo.py", str(tmp_path))
        assert result == str(tmp_path / "foo.py")

    def test_head_with_count(self, tmp_path):
        (tmp_path / "foo.py").touch()
        result = _extract_source_file("head -100 foo.py", str(tmp_path))
        assert result == str(tmp_path / "foo.py")

    def test_sed_print_range(self, tmp_path):
        (tmp_path / "foo.py").touch()
        result = _extract_source_file("sed -n '1,20p' foo.py", str(tmp_path))
        assert result == str(tmp_path / "foo.py")

    def test_pipe_excluded(self, tmp_path):
        assert _extract_source_file("cat foo.py | grep bar", str(tmp_path)) is None

    def test_non_source_file(self, tmp_path):
        (tmp_path / "foo.txt").touch()
        assert _extract_source_file("cat foo.txt", str(tmp_path)) is None

    def test_git_command(self, tmp_path):
        assert _extract_source_file("git log", str(tmp_path)) is None

    def test_empty_command(self, tmp_path):
        assert _extract_source_file("", str(tmp_path)) is None

    def test_rtk_prefix(self, tmp_path):
        (tmp_path / "foo.py").touch()
        result = _extract_source_file("rtk cat foo.py", str(tmp_path))
        assert result == str(tmp_path / "foo.py")

    def test_rtk_head(self, tmp_path):
        (tmp_path / "foo.py").touch()
        result = _extract_source_file("rtk head -20 foo.py", str(tmp_path))
        assert result == str(tmp_path / "foo.py")

    def test_multiple_extensions(self, tmp_path):
        for ext in ['.ts', '.rs', '.go', '.c', '.java']:
            f = tmp_path / f"test{ext}"
            f.touch()
            result = _extract_source_file(f"cat test{ext}", str(tmp_path))
            assert result == str(f), f"Failed for {ext}"

    def test_cd_prefix(self, tmp_path):
        sub = tmp_path / "repo"
        sub.mkdir()
        (sub / "foo.py").touch()
        result = _extract_source_file(f"cd {sub} && cat foo.py", str(tmp_path))
        assert result == str(sub / "foo.py")


# --- Graph DB ---

class TestFindGraphDb:
    def test_found(self, graph_repo):
        assert _find_graph_db(str(graph_repo)) is not None

    def test_walks_up(self, graph_repo):
        sub = graph_repo / "deep" / "sub"
        sub.mkdir(parents=True)
        result = _find_graph_db(str(sub))
        assert result is not None
        assert "graph.db" in str(result)

    def test_not_found(self, tmp_path):
        assert _find_graph_db(str(tmp_path)) is None


class TestResolveFunctions:
    def test_all_functions(self, graph_repo):
        db = _find_graph_db(str(graph_repo))
        fp = str(graph_repo / "src" / "main.py")
        fns = _resolve_functions(db, fp, None, None)
        assert len(fns) == 1
        assert fns[0]['name'] == 'foo'
        assert fns[0]['callers'] == 2
        assert fns[0]['tests'] == 1

    def test_line_range_match(self, graph_repo):
        db = _find_graph_db(str(graph_repo))
        fp = str(graph_repo / "src" / "main.py")
        fns = _resolve_functions(db, fp, 5, 15)
        assert len(fns) == 1
        assert fns[0]['name'] == 'foo'

    def test_line_range_miss(self, graph_repo):
        db = _find_graph_db(str(graph_repo))
        fp = str(graph_repo / "src" / "main.py")
        fns = _resolve_functions(db, fp, 100, 200)
        assert len(fns) == 0

    def test_no_nodes(self, graph_repo):
        db = _find_graph_db(str(graph_repo))
        fns = _resolve_functions(db, "/nonexistent/file.py", None, None)
        assert len(fns) == 0


# --- Cairn DB ---

class TestQueryCairn:
    def test_correction_returned(self, cairn_db, tmp_path):
        results = _query_cairn(cairn_db, f"{tmp_path}/src/main.py", [])
        types = [r['type'] for r in results]
        assert 'correction' in types

    def test_low_confidence_excluded(self, cairn_db, tmp_path):
        results = _query_cairn(cairn_db, f"{tmp_path}/src/main.py", [])
        for r in results:
            assert 'Maybe wrong' not in r['content']

    def test_deleted_excluded(self, cairn_db, tmp_path):
        results = _query_cairn(cairn_db, f"{tmp_path}/src/main.py", [])
        for r in results:
            assert 'Old fix' not in r['content']

    def test_wrong_type_excluded(self, cairn_db, tmp_path):
        results = _query_cairn(cairn_db, f"{tmp_path}/src/main.py", [])
        types = [r['type'] for r in results]
        assert 'fact' not in types

    def test_max_two(self, cairn_db, tmp_path):
        results = _query_cairn(cairn_db, f"{tmp_path}/src/main.py", [])
        assert len(results) <= 2


# --- Footer formatting ---

class TestFormatFooter:
    def test_basic(self):
        fns = [{'name': 'foo', 'callers': 5, 'tests': 2, 'qualified_name': 'x::foo'}]
        result = _format_footer(fns, [])
        assert 'foo: 5 callers' in result
        assert '2 tests' in result

    def test_with_memory(self):
        fns = [{'name': 'bar', 'callers': 3, 'tests': 1, 'qualified_name': 'x::bar'}]
        mem = [{'type': 'correction', 'content': 'Fixed bug', 'updated_at': '2026-05-02 10:00:00'}]
        result = _format_footer(fns, mem)
        assert 'correction' in result
        assert 'Fixed bug' in result

    def test_truncation(self):
        fns = [{'name': 'baz', 'callers': 5, 'tests': 2, 'qualified_name': 'x::baz'}]
        mem = [{'type': 'correction', 'content': 'A' * 200, 'updated_at': '2026-05-02 10:00:00'}]
        result = _format_footer(fns, mem)
        assert len(result) <= 200

    def test_no_data_returns_none(self):
        assert _format_footer([], []) is None

    def test_memories_only(self):
        mem = [{'type': 'decision', 'content': 'Use X', 'updated_at': '2026-05-02 10:00:00'}]
        result = _format_footer([], mem)
        assert result is not None
        assert 'decision' in result

    def test_multiple_functions(self):
        fns = [
            {'name': 'foo', 'callers': 2, 'tests': 1, 'qualified_name': 'x::foo'},
            {'name': 'bar', 'callers': 0, 'tests': 0, 'qualified_name': 'x::bar'},
        ]
        result = _format_footer(fns, [])
        assert 'foo:' in result
        assert 'bar:' in result


# --- Integration ---

class TestGenerateFooter:
    def test_full_pipeline(self, graph_repo):
        if _SESSION_SEEN_FILE.exists():
            _SESSION_SEEN_FILE.unlink()
        fp = str(graph_repo / "src" / "main.py")
        result = generate_footer(f"cat {fp}", str(graph_repo))
        assert result is not None
        assert "foo:" in result
        assert "2 callers" in result
        assert "1 tests" in result

    def test_dedup(self, graph_repo):
        """Second call for same file returns None (dedup)."""
        if _SESSION_SEEN_FILE.exists():
            _SESSION_SEEN_FILE.unlink()
        fp = str(graph_repo / "src" / "main.py")
        result1 = generate_footer(f"cat {fp}", str(graph_repo))
        assert result1 is not None
        result2 = generate_footer(f"cat {fp}", str(graph_repo))
        assert result2 is None
        _SESSION_SEEN_FILE.unlink(missing_ok=True)

    def test_sed_line_range(self, graph_repo):
        """sed -n with range overlapping foo (lines 1-20) shows foo."""
        if _SESSION_SEEN_FILE.exists():
            _SESSION_SEEN_FILE.unlink()
        fp = str(graph_repo / "src" / "main.py")
        result = generate_footer(f"sed -n '5,10p' {fp}", str(graph_repo))
        assert result is not None
        assert "foo:" in result
        _SESSION_SEEN_FILE.unlink(missing_ok=True)

    def test_sed_line_range_miss(self, graph_repo):
        """sed -n with range outside any function returns None."""
        if _SESSION_SEEN_FILE.exists():
            _SESSION_SEEN_FILE.unlink()
        fp = str(graph_repo / "src" / "main.py")
        result = generate_footer(f"sed -n '100,200p' {fp}", str(graph_repo))
        assert result is None
        _SESSION_SEEN_FILE.unlink(missing_ok=True)

    def test_no_graph_returns_none(self, tmp_path):
        (tmp_path / "foo.py").touch()
        result = generate_footer("cat foo.py", str(tmp_path))
        assert result is None

    def test_non_read_returns_none(self, graph_repo):
        assert generate_footer("git log", str(graph_repo)) is None

    def test_never_raises(self, tmp_path):
        gdir = tmp_path / ".code-review-graph"
        gdir.mkdir()
        (gdir / "graph.db").write_text("not a database")
        (tmp_path / "foo.py").touch()
        result = generate_footer("cat foo.py", str(tmp_path))
        assert result is None


class TestExtractLineRange:
    def test_sed_basic(self):
        assert _extract_line_range("sed -n '10,25p' foo.py") == (10, 25)

    def test_sed_double_quotes(self):
        assert _extract_line_range('sed -n "10,25p" foo.py') == (10, 25)

    def test_sed_rtk_prefix(self):
        assert _extract_line_range("rtk sed -n '100,150p' bar.py") == (100, 150)

    def test_head_n(self):
        assert _extract_line_range("head -n 20 foo.py") == (1, 20)

    def test_head_dash_n(self):
        assert _extract_line_range("head -n20 foo.py") == (1, 20)

    def test_head_shortform(self):
        assert _extract_line_range("head -20 foo.py") == (1, 20)

    def test_cat_no_range(self):
        assert _extract_line_range("cat foo.py") == (None, None)

    def test_non_read(self):
        assert _extract_line_range("git log") == (None, None)


class TestSessionDedup:
    def test_load_save(self):
        if _SESSION_SEEN_FILE.exists():
            _SESSION_SEEN_FILE.unlink()
        from lib.cairn_graph_footer import _save_seen, _load_seen
        assert _load_seen() == set()
        _save_seen({'a::foo', 'b::bar'})
        assert _load_seen() == {'a::foo', 'b::bar'}
        _SESSION_SEEN_FILE.unlink(missing_ok=True)
