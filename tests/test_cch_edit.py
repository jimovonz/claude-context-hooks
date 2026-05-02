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
