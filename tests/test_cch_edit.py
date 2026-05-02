"""Tests for cch-edit.py — Bash-routed alternative to built-in Edit."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / 'hooks' / 'cch-edit.py'


def _run(*args):
    proc = subprocess.run(
        [sys.executable, str(HELPER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


@pytest.fixture
def target(tmp_path):
    p = tmp_path / 'target.txt'
    p.write_text('hello world\n')
    return p


def test_single_line_replace(target):
    rc, out, err = _run(str(target), 'hello', 'goodbye')
    assert rc == 0
    assert target.read_text() == 'goodbye world\n'
    assert '+goodbye world' in out
    assert 'replaced 1 occurrence' in out


def test_replace_preserves_surrounding_content(target):
    target.write_text('a\nfoo\nb\n')
    rc, out, err = _run(str(target), 'foo', 'bar')
    assert rc == 0
    assert target.read_text() == 'a\nbar\nb\n'


def test_uniqueness_check_blocks_multiple(target):
    target.write_text('foo bar foo\n')
    rc, out, err = _run(str(target), 'foo', 'baz')
    assert rc == 1
    assert 'appears 2 times' in err
    assert target.read_text() == 'foo bar foo\n'  # unchanged


def test_all_flag_replaces_every_occurrence(target):
    target.write_text('foo bar foo\n')
    rc, out, err = _run(str(target), 'foo', 'baz', '--all')
    assert rc == 0
    assert target.read_text() == 'baz bar baz\n'
    assert 'replaced 2 occurrence' in out


def test_old_string_not_found_errors(target):
    rc, out, err = _run(str(target), 'xyz', 'abc')
    assert rc == 1
    assert 'not found' in err
    assert target.read_text() == 'hello world\n'


def test_identical_old_and_new_errors(target):
    rc, out, err = _run(str(target), 'hello', 'hello')
    assert rc == 1
    assert 'identical' in err


def test_empty_old_string_errors(target):
    rc, out, err = _run(str(target), '', 'something')
    assert rc == 1
    assert 'empty' in err.lower()


def test_missing_target_errors(tmp_path):
    rc, out, err = _run(str(tmp_path / 'nope.txt'), 'a', 'b')
    assert rc == 1
    assert 'not a regular file' in err


def test_old_file_and_new_file(tmp_path, target):
    target.write_text('alpha\nbeta\ngamma\n')
    old_f = tmp_path / 'old.txt'
    new_f = tmp_path / 'new.txt'
    old_f.write_text('alpha\nbeta')
    new_f.write_text('ALPHA\nBETA')
    rc, out, err = _run(str(target), '--old-file', str(old_f), '--new-file', str(new_f))
    assert rc == 0
    assert target.read_text() == 'ALPHA\nBETA\ngamma\n'


def test_atomic_no_partial_on_error(tmp_path):
    """If tmp file exists from a previous failed run, helper still
    succeeds and cleans up — no .cch-tmp leak after success."""
    target = tmp_path / 't.txt'
    target.write_text('hello\n')
    rc, out, err = _run(str(target), 'hello', 'goodbye')
    assert rc == 0
    leftovers = list(tmp_path.glob('*.cch-tmp'))
    assert leftovers == []


def test_preserves_executable_mode(tmp_path):
    """cch-edit must NOT strip the executable bit on edit. Regression
    against the 2026-05-02 incident where editing cache-wrap.py via
    cch-edit dropped 0775 to 0664 and broke the entire Bash chain."""
    target = tmp_path / 't.sh'
    target.write_text('#!/bin/sh\necho hello\n')
    target.chmod(0o755)
    rc, out, err = _run(str(target), 'hello', 'goodbye')
    assert rc == 0
    mode = target.stat().st_mode & 0o777
    assert mode == 0o755, f'expected 0o755, got {oct(mode)}'



# --- Impact line tests ---------------------------------------------------

NODES_DDL = """
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

EDGES_DDL = """
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    confidence REAL DEFAULT 1.0,
    confidence_tier TEXT DEFAULT 'EXTRACTED',
    updated_at REAL NOT NULL
)
"""


import sqlite3


def _create_impact_db(root, rel_path, nodes, edges=None):
    """Create .code-review-graph/graph.db with nodes and edges tables.

    *nodes*: list of (kind, name, qualified_name, file_path, line_start, line_end)
    *edges*: list of (kind, source_qualified, target_qualified, file_path)
    """
    db_dir = root / ".code-review-graph"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(NODES_DDL)
    conn.execute(EDGES_DDL)
    for n in nodes:
        kind, name, qname, fpath, ls, le = n
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, "
            "line_start, line_end, language, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'python', 0.0)",
            (kind, name, qname, fpath, ls, le),
        )
    for e in (edges or []):
        kind, src, tgt, fpath = e
        conn.execute(
            "INSERT INTO edges (kind, source_qualified, target_qualified, "
            "file_path, updated_at) VALUES (?, ?, ?, ?, 0.0)",
            (kind, src, tgt, fpath),
        )
    conn.commit()
    conn.close()
    return db_path


def test_impact_line_printed(tmp_path):
    """When graph.db exists and the edit touches a known function, the
    impact line is printed with caller/test counts."""
    # Create a source file inside tmp_path (repo root)
    src = tmp_path / "mod.py"
    src.write_text("def greet():\n    return 'hello'\n\ndef other():\n    pass\n")

    # Build graph.db with one function node and one caller edge
    _create_impact_db(
        tmp_path,
        "mod.py",
        nodes=[
            ("Function", "greet", "mod.greet", "mod.py", 1, 2),
        ],
        edges=[
            ("CALLS", "app.main", "mod.greet", "app.py"),
            ("TESTED_BY", "mod.greet", "tests.test_greet", "tests.py"),
        ],
    )

    rc, out, err = _run(str(src), "hello", "goodbye")
    assert rc == 0
    assert "impact:" in out
    assert "callers:1" in out
    assert "tests:1" in out


def test_impact_line_no_graph(tmp_path):
    """Without graph.db, the edit still succeeds and no impact line appears."""
    src = tmp_path / "mod.py"
    src.write_text("def greet():\n    return 'hello'\n")

    rc, out, err = _run(str(src), "hello", "goodbye")
    assert rc == 0
    assert "impact:" not in out
    assert "replaced 1 occurrence" in out

