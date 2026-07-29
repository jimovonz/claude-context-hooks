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
    assert 'Net observed:' in out


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
    # Big cmd: stub 200; small cmd: emitted in full (no saving)
    assert cw['emitted_bytes'] == 200 + 50
    # Avoided = 100_050 - 250 = 99_800 bytes -> ~24_950 tokens
    saved = cw['original_bytes'] - cw['emitted_bytes']
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
        {'ts': _now_iso(), 'event': 'deny_grep', 'pattern': 'foo', 'sid': 's1'},
        {'ts': _now_iso(), 'event': 'deny_glob', 'pattern': '*.py', 'sid': 's1'},
        {'ts': _now_iso(), 'event': 'deny_webfetch', 'url': 'https://x', 'sid': 's1'},
    ])
    rc, out, err = _run([], tmp_path)
    assert 'Grep/Glob/WebFetch' in out
    assert 'no saving claimed' in out
    # Every deny is a correction, and corrections are the acceptance signal.
    assert 'deny_grep' in out and 'deny_glob' in out and 'deny_webfetch' in out
    assert 'FRICTION' in out


def test_methodology_tags_present(tmp_path):
    _seed_events(tmp_path, [
        {'ts': _now_iso(), 'event': 'cache_wrap',
         'cmd_head': 'x', 'original_bytes': 100, 'stub_bytes': None,
         'cached': False, 'threshold': 8000},
    ])
    rc, out, err = _run([], tmp_path)
    assert 'AVOIDED (observed)' in out
    assert 'PAID (observed)' in out
    assert 'COUNTERFACTUAL (modelled, NOT included in the net above)' in out
    assert '[st_size]' in out


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


def test_retrieval_retry_detection(tmp_path):
    """A similar Bash command within the retry window after a retrieval
    is flagged as a retry — slice didn't satisfy."""
    from datetime import timedelta
    base = datetime.now() - timedelta(minutes=2)
    full_key = 'b2s:abcd1234567890123456'
    truncated = full_key[:20] + '...'
    # Cache a find /usr command, retrieve a slice, then re-issue find /usr
    _seed_events(tmp_path, [
        {'ts': base.isoformat(timespec='seconds'),
         'event': 'cache_wrap', 'cmd_head': 'find /usr -type f',
         'original_bytes': 100_000, 'stub_bytes': 200, 'cached': True,
         'cache_key': full_key, 'threshold': 2000},
        {'ts': (base + timedelta(seconds=30)).isoformat(timespec='seconds'),
         'event': 'cache_wrap', 'cmd_head': 'find /usr -type d -name lib',
         'original_bytes': 5000, 'stub_bytes': None, 'cached': False,
         'threshold': 2000},
    ])
    _seed_retrievals(tmp_path, [
        {'timestamp': (base + timedelta(seconds=10)).isoformat(),
         'key': truncated, 'source_size': 100_000, 'returned_bytes': 500,
         'is_full_retrieval': False},
    ])
    rc, out, err = _run(['--retrieval', '--days', '1'], tmp_path)
    assert rc == 0
    # Both events have cmd signature ('find', '/usr') so retry flagged
    assert "1 retries" in out


def test_retrieval_no_retry_when_unrelated(tmp_path):
    """Subsequent unrelated Bash command should NOT be flagged as retry."""
    from datetime import timedelta
    base = datetime.now() - timedelta(minutes=2)
    full_key = 'b2s:abcd1234567890123456'
    truncated = full_key[:20] + '...'
    _seed_events(tmp_path, [
        {'ts': base.isoformat(timespec='seconds'),
         'event': 'cache_wrap', 'cmd_head': 'find /usr -type f',
         'original_bytes': 100_000, 'stub_bytes': 200, 'cached': True,
         'cache_key': full_key, 'threshold': 2000},
        {'ts': (base + timedelta(seconds=30)).isoformat(timespec='seconds'),
         'event': 'cache_wrap', 'cmd_head': 'git status',
         'original_bytes': 200, 'stub_bytes': None, 'cached': False,
         'threshold': 2000},
    ])
    _seed_retrievals(tmp_path, [
        {'timestamp': (base + timedelta(seconds=10)).isoformat(),
         'key': truncated, 'source_size': 100_000, 'returned_bytes': 500,
         'is_full_retrieval': False},
    ])
    rc, out, err = _run(['--retrieval', '--days', '1'], tmp_path)
    assert rc == 0
    assert "0 retries" in out


def _seed_retrievals(home: Path, rows):
    log = home / '.claude' / 'cache' / 'ccm' / 'retrieval.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_outline_report_separates_indexed_from_unindexed(tmp_path):
    """The hypothesis test: does a stub index change retrieval behaviour?"""
    ts = _now_iso()
    _seed_events(tmp_path, [
        {'ts': ts, 'event': 'cache_wrap', 'cmd_head': 'cat a', 'cached': True,
         'cache_key': 'b2s:aaa', 'original_bytes': 50_000, 'stub_bytes': 300,
         'outline_sections': 4, 'outline_bytes': 120, 'has_menu': False},
        {'ts': ts, 'event': 'cache_wrap', 'cmd_head': 'cat b', 'cached': True,
         'cache_key': 'b2s:bbb', 'original_bytes': 50_000, 'stub_bytes': 200,
         'outline_sections': 0, 'outline_bytes': 40, 'has_menu': False},
        {'ts': ts, 'event': 'cache_wrap', 'cmd_head': 'cat c.py', 'cached': True,
         'cache_key': 'b2s:ccc', 'original_bytes': 50_000, 'stub_bytes': 260,
         'outline_sections': 0, 'outline_bytes': 40, 'has_menu': True},
    ])
    _seed_retrievals(tmp_path, [
        {'timestamp': ts, 'key': 'b2s:aaa', 'filter': {'lines': '10-20'},
         'source_size': 50_000, 'returned_bytes': 5_000},
        {'timestamp': ts, 'key': 'b2s:bbb', 'filter': {'grep': '.'},
         'source_size': 50_000, 'returned_bytes': 50_000},
    ])

    rc, out, err = _run(['--outline'], tmp_path)
    assert rc == 0, err
    assert 'sections index' in out
    assert 'profile only' in out
    assert 'symbol menu' in out
    # the indexed blob was sliced; the unindexed one was pulled whole
    assert '0.10' in out and '1.00' in out
    assert 'lines 100%' in out


def test_outline_report_survives_empty_window(tmp_path):
    _seed_events(tmp_path, [])
    rc, out, err = _run(['--outline'], tmp_path)
    assert rc == 0, err
    assert 'no cached events in window' in out
