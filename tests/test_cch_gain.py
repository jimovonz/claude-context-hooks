"""Tests for cch-gain.py — aggregation, methodology tags, JSON output."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GAIN = REPO_ROOT / 'hooks' / 'cch-gain.py'


def _run(args, home: Path):
    env = os.environ.copy()
    env['HOME'] = str(home)
    proc = subprocess.run(
        [sys.executable, str(GAIN), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=15,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


def _seed_events(home: Path, rows):
    log = home / '.claude' / 'cache' / 'ccm' / 'events.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def _now_iso():
    return datetime.now().isoformat(timespec='seconds')


def test_empty_log_zero_savings(tmp_path):
    rc, out, err = _run([], tmp_path)
    assert rc == 0
    assert 'Total honest savings: ~0 tokens' in out


def test_cache_wrap_savings_counted(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'find /', 'original_bytes': 100_000,
         'stub_bytes': 200, 'cached': True, 'threshold': 8000},
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'echo hi', 'original_bytes': 50,
         'stub_bytes': None, 'cached': False, 'threshold': 8000},
    ])
    rc, out, err = _run(['--json'], tmp_path)
    assert rc == 0
    data = json.loads(out)
    cw = data['cache_wrap']
    assert cw['cmds'] == 2
    assert cw['cached'] == 1
    assert cw['original_bytes'] == 100_050
    # Big cmd: stub 200; small cmd: counted as original (no saving)
    assert cw['stub_bytes'] == 200 + 50
    # Saved = 100_050 - 250 = 99_800 bytes -> ~24_950 tokens
    saved = cw['original_bytes'] - cw['stub_bytes']
    assert saved == 99_800


def test_deny_read_counterfactual(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'deny_read', 'path': '/a.txt', 'st_size': 4096},
        {'ts': _now_iso(), 'event': 'deny_read', 'path': '/b.txt', 'st_size': 8192},
    ])
    rc, out, err = _run(['--json'], tmp_path)
    data = json.loads(out)
    assert data['deny_read']['count'] == 2
    assert data['deny_read']['st_size_total'] == 4096 + 8192


def test_deny_edit_write_aggregation(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'deny_edit', 'path': '/x.py', 'st_size': 1000},
        {'ts': _now_iso(), 'event': 'deny_write', 'path': '/y.py', 'st_size_existing': 500},
        {'ts': _now_iso(), 'event': 'deny_write', 'path': '/new.py', 'st_size_existing': 0},
        {'ts': _now_iso(), 'event': 'deny_notebookedit', 'path': '/n.ipynb', 'st_size': 2000},
    ])
    rc, out, err = _run(['--json'], tmp_path)
    data = json.loads(out)
    # New-file Write does NOT count (no read-tax to save)
    assert data['deny_edit_write']['count'] == 3
    assert data['deny_edit_write']['st_size_total'] == 1000 + 500 + 2000


def test_grep_glob_webfetch_counts_no_savings(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'deny_grep', 'pattern': 'foo'},
        {'ts': _now_iso(), 'event': 'deny_glob', 'pattern': '*.py'},
        {'ts': _now_iso(), 'event': 'deny_webfetch', 'url': 'https://x'},
    ])
    rc, out, err = _run([], tmp_path)
    assert 'Grep denies:          1' in out
    assert 'Glob denies:          1' in out
    assert 'WebFetch denies:      1' in out
    assert 'no direct saving claimed' in out
    assert 'savings not measurable' in out


def test_methodology_tags_present(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'x', 'original_bytes': 100, 'stub_bytes': None,
         'cached': False, 'threshold': 8000},
    ])
    rc, out, err = _run([], tmp_path)
    assert '[observed]' in out
    assert '[counterfactual: st_size]' in out
    assert '[counterfactual: read-tax st_size]' in out


def test_downstream_avoidance_caveat_in_footer(tmp_path):
    rc, out, err = _run([], tmp_path)
    assert 'CCH_DISABLE=1' in out
    assert 'Invisible downstream' in out or 'invisible downstream' in out.lower()


def test_window_filter_excludes_old_events(tmp_path):
    old = (datetime.now() - timedelta(days=60)).isoformat(timespec='seconds')
    new = _now_iso()
    _seed_events(tmp_path, [
        {'ts': old, 'event': 'deny_read', 'path': '/old', 'st_size': 100_000},
        {'ts': new, 'event': 'deny_read', 'path': '/new', 'st_size': 1000},
    ])
    rc, out, err = _run(['--days', '7', '--json'], tmp_path)
    data = json.loads(out)
    assert data['deny_read']['count'] == 1
    assert data['deny_read']['st_size_total'] == 1000


def test_since_flag_overrides_days(tmp_path):
    rc, out, err = _run(['--since', '2020-01-01'], tmp_path)
    assert rc == 0
    assert 'since 2020-01-01' in out


def test_invalid_since_errors(tmp_path):
    rc, out, err = _run(['--since', 'not-a-date'], tmp_path)
    assert rc == 1
    assert 'invalid' in err.lower()
