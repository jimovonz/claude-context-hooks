"""Tests for ccm-get.py --symbol feature (graph.db symbol resolution)."""

import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Import _resolve_symbol_lines from ccm-get (hyphenated module name)
sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
ccm_get = importlib.import_module('ccm-get')
_resolve_symbol_lines = ccm_get._resolve_symbol_lines

NODES_DDL = """\
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
)
"""


def _create_graph_db(root: Path, rows: list[tuple] | None = None) -> Path:
    """Create a .code-review-graph/graph.db under *root* with optional node rows."""
    db_dir = root / ".code-review-graph"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(NODES_DDL)
    if rows:
        conn.executemany(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, "
            "line_start, line_end, language, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()
    conn.close()
    return db_path


class TestResolveSymbolLines:
    """Unit tests for _resolve_symbol_lines()."""

    def test_found(self, tmp_path, monkeypatch):
        """Symbol present in graph.db returns correct (start, end) tuple."""
        _create_graph_db(
            tmp_path,
            rows=[
                # kind, name, qualified_name, file_path, line_start, line_end, language, updated_at
                ("Function", "my_func", "mod.my_func", "mod.py", 10, 25, "python", 0.0),
            ],
        )
        monkeypatch.chdir(tmp_path)

        result = _resolve_symbol_lines("my_func")
        assert result == (10, 25)

    def test_class_kind_found(self, tmp_path, monkeypatch):
        """Class nodes are also resolved."""
        _create_graph_db(
            tmp_path,
            rows=[
                ("Class", "MyClass", "mod.MyClass", "mod.py", 1, 50, "python", 0.0),
            ],
        )
        monkeypatch.chdir(tmp_path)

        result = _resolve_symbol_lines("MyClass")
        assert result == (1, 50)

    def test_test_kind_found(self, tmp_path, monkeypatch):
        """Test nodes are also resolved."""
        _create_graph_db(
            tmp_path,
            rows=[
                ("Test", "test_something", "tests.test_something", "tests.py", 5, 15, "python", 0.0),
            ],
        )
        monkeypatch.chdir(tmp_path)

        result = _resolve_symbol_lines("test_something")
        assert result == (5, 15)

    def test_not_found(self, tmp_path, monkeypatch):
        """No matching node returns None."""
        _create_graph_db(tmp_path, rows=[])
        monkeypatch.chdir(tmp_path)

        result = _resolve_symbol_lines("nonexistent")
        assert result is None

    def test_wrong_kind_excluded(self, tmp_path, monkeypatch):
        """Nodes with kinds outside (Function, Class, Test) are not returned."""
        _create_graph_db(
            tmp_path,
            rows=[
                ("Module", "helpers", "helpers", "helpers.py", 1, 100, "python", 0.0),
            ],
        )
        monkeypatch.chdir(tmp_path)

        result = _resolve_symbol_lines("helpers")
        assert result is None

    def test_no_db(self, tmp_path, monkeypatch):
        """No graph.db present returns None without crashing."""
        monkeypatch.chdir(tmp_path)

        result = _resolve_symbol_lines("anything")
        assert result is None

    def test_walks_up_to_find_db(self, tmp_path, monkeypatch):
        """graph.db in an ancestor directory is found when cwd is deeper."""
        _create_graph_db(
            tmp_path,
            rows=[
                ("Function", "deep_func", "pkg.sub.deep_func", "pkg/sub/mod.py", 20, 40, "python", 0.0),
            ],
        )
        # Create a nested subdirectory and chdir into it
        nested = tmp_path / "pkg" / "sub"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        result = _resolve_symbol_lines("deep_func")
        assert result == (20, 40)
