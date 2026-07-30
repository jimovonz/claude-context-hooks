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


# --- prune -------------------------------------------------------------
# The index was the one store the review pass left unswept: blobs got TTL and
# size caps, markers got nothing. Eviction here keys on blob-absence, because
# may_elide() cannot fire without the old key's blob.

def test_prune_keeps_entries_whose_old_blob_is_still_cached(index, monkeypatch):
    monkeypatch.setattr(index, '_cached', lambda key: True)
    index.record('b2s:aaaa', 'b2s:bbbb')
    result = index.prune()
    assert result['removed'] == 0
    assert index.superseded_by('b2s:aaaa') == 'b2s:bbbb'


def test_prune_drops_entries_whose_old_blob_is_gone(index, monkeypatch):
    monkeypatch.setattr(index, '_cached', lambda key: False)
    index.record('b2s:aaaa', 'b2s:bbbb')
    result = index.prune()
    assert result['removed'] == 1
    assert index.superseded_by('b2s:aaaa') is None


def test_prune_keeps_interior_links_of_a_live_chain(index, monkeypatch):
    # K1->K2->K3 with only K1 still cached. Dropping K2 would truncate K1's
    # chain and silently cost a legitimate elision.
    monkeypatch.setattr(index, '_cached', lambda key: key == 'b2s:aaaa')
    index.record('b2s:aaaa', 'b2s:bbbb')
    index.record('b2s:bbbb', 'b2s:cccc')
    index.prune()
    assert index.chain('b2s:aaaa') == ['b2s:bbbb', 'b2s:cccc']


def test_prune_falls_back_to_age_when_the_blob_lookup_is_unavailable(index, monkeypatch):
    import time as _time
    monkeypatch.setattr(index, '_blob_lookup_available', lambda: False)
    monkeypatch.setattr(index, '_cached', lambda key: False)
    index.record('b2s:aaaa', 'b2s:bbbb')
    marker = index.SUPERSEDED_DIR / 'aaaa'
    # Fresh marker survives even though _cached is False, because with no
    # lookup we cannot tell absence from unreadability.
    assert index.prune()['removed'] == 0
    old = _time.time() - 30 * 86400
    os.utime(marker, (old, old))
    assert index.prune()['removed'] == 1


def test_prune_on_a_missing_index_dir_is_not_an_error(index):
    assert index.prune()['removed'] == 0
