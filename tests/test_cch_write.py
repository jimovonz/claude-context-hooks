"""Tests for cch-write.py — Bash-routed alternative to built-in Write."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / 'hooks' / 'cch-write.py'


def _run(*args, stdin=b''):
    proc = subprocess.run(
        [sys.executable, str(HELPER), *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


def test_writes_stdin_to_path(tmp_path):
    target = tmp_path / 'out.txt'
    rc, out, err = _run(str(target), stdin=b'hello\n')
    assert rc == 0
    assert target.read_text() == 'hello\n'
    assert 'wrote 6 bytes' in out


def test_overwrites_existing(tmp_path):
    target = tmp_path / 'out.txt'
    target.write_text('previous\n')
    rc, out, err = _run(str(target), stdin=b'replaced\n')
    assert rc == 0
    assert target.read_text() == 'replaced\n'


def test_creates_parent_directories(tmp_path):
    target = tmp_path / 'a' / 'b' / 'c' / 'out.txt'
    rc, out, err = _run(str(target), stdin=b'nested\n')
    assert rc == 0
    assert target.read_text() == 'nested\n'


def test_binary_content_preserved(tmp_path):
    target = tmp_path / 'bin.bin'
    payload = bytes(range(256))
    rc, out, err = _run(str(target), stdin=payload)
    assert rc == 0
    assert target.read_bytes() == payload


def test_empty_content(tmp_path):
    target = tmp_path / 'empty.txt'
    rc, out, err = _run(str(target), stdin=b'')
    assert rc == 0
    assert target.read_bytes() == b''
    assert 'wrote 0 bytes' in out


def test_target_is_dir_errors(tmp_path):
    rc, out, err = _run(str(tmp_path), stdin=b'x')
    assert rc == 1
    assert 'directory' in err


def test_no_args_errors():
    rc, out, err = _run()
    assert rc == 1
    assert 'Usage' in err


def test_no_temp_leak_on_success(tmp_path):
    target = tmp_path / 'out.txt'
    rc, out, err = _run(str(target), stdin=b'data\n')
    assert rc == 0
    leftovers = list(tmp_path.glob('*.cch-tmp'))
    assert leftovers == []
