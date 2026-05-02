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


def test_dist_histogram(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'a', 'original_bytes': 100, 'stub_bytes': None,
         'cached': False, 'threshold': 8000},
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'b', 'original_bytes': 5000, 'stub_bytes': None,
         'cached': False, 'threshold': 8000},
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'c', 'original_bytes': 20000, 'stub_bytes': 200,
         'cached': True, 'threshold': 8000},
    ])
    rc, out, err = _run(['--dist', '--days', '1'], tmp_path)
    assert rc == 0
    assert 'Cache wrapper distribution' in out
    assert 'n = 3' in out
    assert '<-- current default' in out
    # Threshold trial: > 4000 bytes catches 2 (the 5000 and 20000 events)
    assert '> 4000 bytes' in out
    # > 8000 bytes catches 1 (the 20000 event)
    lines_with_8000 = [l for l in out.splitlines() if '> 8000' in l]
    assert any('1' in l for l in lines_with_8000)


def test_dist_json(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'a', 'original_bytes': 1500, 'stub_bytes': None,
         'cached': False, 'threshold': 8000},
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'b', 'original_bytes': 9000, 'stub_bytes': 200,
         'cached': True, 'threshold': 8000},
    ])
    rc, out, err = _run(['--dist', '--json'], tmp_path)
    assert rc == 0
    data = json.loads(out)
    assert data['n'] == 2
    by_t = {t['threshold']: t['would_cache'] for t in data['trials']}
    assert by_t[1000] == 2  # both above 1000
    assert by_t[2000] == 1  # only 9000 above 2000
    assert by_t[8000] == 1


def test_dist_empty_log(tmp_path):
    rc, out, err = _run(['--dist'], tmp_path)
    assert rc == 0
    assert 'No cache_wrap events' in out


def _seed_retrievals(home: Path, rows):
    log = home / '.claude' / 'cache' / 'ccm' / 'retrieval.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_retrieval_orphan_detection(tmp_path):
    """A cache that was never retrieved is an orphan."""
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'find /', 'original_bytes': 50_000,
         'stub_bytes': 200, 'cached': True, 'cache_key': 'b2s:abcd1234567890123456',
         'threshold': 8000},
    ])
    # No retrievals seeded
    rc, out, err = _run(['--retrieval', '--days', '1'], tmp_path)
    assert rc == 0
    assert 'n = 1 cached events, 1 orphaned' in out
    assert '100%' in out  # orphan rate


def test_retrieval_partial_match(tmp_path):
    """A cache with retrievals computes the ratio correctly."""
    full_key = 'b2s:abcd1234567890123456'
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'big', 'original_bytes': 100_000,
         'stub_bytes': 200, 'cached': True, 'cache_key': full_key,
         'threshold': 8000},
    ])
    # retrieval.log uses key[:20] + '...'
    truncated = full_key[:20] + '...'
    _seed_retrievals(tmp_path, [
        {'timestamp': _now_iso(), 'key': truncated,
         'source_size': 100_000, 'returned_bytes': 5000,
         'is_full_retrieval': False},
    ])
    rc, out, err = _run(['--retrieval', '--days', '1'], tmp_path)
    assert rc == 0
    # 5000/100000 = 5% -> <10% bucket
    assert '0 orphaned' in out
    assert '<10%' in out


def test_retrieval_no_caches_in_window(tmp_path):
    rc, out, err = _run(['--retrieval', '--days', '1'], tmp_path)
    assert rc == 0
    assert 'No cached cache_wrap events in window' in out
