"""Tests for the supersession index — CCH's half of docs/CONTRACT.md."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / 'hooks'
CACHE_WRAP = HOOKS / 'cache-wrap.py'
sys.path.insert(0, str(HOOKS))


@pytest.fixture
def index(tmp_path, monkeypatch):
    from lib import supersede
    monkeypatch.setattr(supersede, 'SUPERSEDED_DIR', tmp_path / 'superseded')
    return supersede


def test_records_and_resolves_one_hop(index):
    assert index.record('b2s:aaa', 'b2s:bbb') is True
    assert index.superseded_by('b2s:aaa') == 'b2s:bbb'
    assert index.superseded_by('b2s:bbb') is None


def test_entries_are_monotone(index):
    """Determinism depends on this: a rewritten entry flips the transform."""
    index.record('b2s:aaa', 'b2s:bbb')
    assert index.record('b2s:aaa', 'b2s:ccc') is False
    assert index.superseded_by('b2s:aaa') == 'b2s:bbb'


def test_chain_is_transitive_and_cycle_safe(index):
    index.record('b2s:k1', 'b2s:k2')
    index.record('b2s:k2', 'b2s:k3')
    assert index.chain('b2s:k1') == ['b2s:k2', 'b2s:k3']

    index.record('b2s:c1', 'b2s:c2')
    index.record('b2s:c2', 'b2s:c1')
    assert len(index.chain('b2s:c1')) <= index.MAX_CHAIN


def test_may_elide_requires_the_replacement_to_be_present(index, monkeypatch):
    """Clause 3 is the whole safety argument — without it this is eviction."""
    monkeypatch.setattr(index, '_cached', lambda key: True)
    index.record('b2s:old', 'b2s:new')

    assert index.may_elide('b2s:old', ['b2s:new']) is True
    assert index.may_elide('b2s:old', ['b2s:unrelated']) is False
    assert index.may_elide('b2s:old', []) is False


def test_may_elide_requires_supersession(index, monkeypatch):
    monkeypatch.setattr(index, '_cached', lambda key: True)
    assert index.may_elide('b2s:merely_old', ['b2s:something']) is False


def test_may_elide_requires_content_still_cached(index, monkeypatch):
    """Never elide what can no longer be fetched back."""
    index.record('b2s:old', 'b2s:new')
    monkeypatch.setattr(index, '_cached', lambda key: False)
    assert index.may_elide('b2s:old', ['b2s:new']) is False


def test_may_elide_accepts_a_later_link_in_the_chain(index, monkeypatch):
    monkeypatch.setattr(index, '_cached', lambda key: True)
    index.record('b2s:k1', 'b2s:k2')
    index.record('b2s:k2', 'b2s:k3')
    assert index.may_elide('b2s:k1', ['b2s:k3']) is True


def test_supersession_is_recorded_by_a_real_re_read(tmp_path):
    """End to end: re-running a command whose output changed records it."""
    target = tmp_path / 'data.txt'
    target.write_text('\n'.join(f'line {i}' for i in range(400)) + '\n')
    env = os.environ.copy()
    env.update({'HOME': str(tmp_path), 'CCH_SESSION_ID': 'supersede-test',
                'CCH_CACHE_THRESHOLD': '100000'})

    def run():
        p = subprocess.run([sys.executable, str(CACHE_WRAP), '--', f'cat {target}'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env=env, timeout=60)
        return p.stdout.decode('utf-8', 'replace')

    run()
    target.write_text(target.read_text().replace('line 5\n', 'CHANGED\n'))
    second = run()
    assert 'CCM_DELTA' in second

    idx = tmp_path / '.claude' / 'cache' / 'cch' / 'superseded'
    entries = list(idx.iterdir()) if idx.exists() else []
    assert entries, 'a changed re-read should record supersession'
    assert entries[0].read_text().startswith('b2s:')
