"""End-to-end tests for cache-wrap.py.

Runs the wrapper as a subprocess so we exercise the real bash exec
path. HOME is redirected to tmp_path so the ccm cache lands there.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / 'hooks' / 'cache-wrap.py'


def _run(inner: str, tmp_path: Path, threshold: int | None = None):
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)
    if threshold is not None:
        env['CCH_CACHE_THRESHOLD'] = str(threshold)
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), '--', inner],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=10,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


def test_small_output_inlined(tmp_path):
    """Below threshold → output passes through unchanged."""
    rc, out, err = _run('echo hello', tmp_path)
    assert rc == 0
    assert out == 'hello\n'
    assert '[CCM_CACHED]' not in out


def test_large_output_cached_and_stubbed(tmp_path):
    """Above threshold → stub on stdout, blob in cache."""
    inner = 'python3 -c "print(\\"x\\" * 20000)"'
    rc, out, err = _run(inner, tmp_path, threshold=1000)
    assert rc == 0
    assert '[CCM_CACHED]' in out
    assert '[/CCM_CACHED]' in out
    assert 'Retrieve: ccm-get.py' in out
    blobs = list((tmp_path / '.claude' / 'cache' / 'ccm' / 'blobs').iterdir())
    assert len(blobs) == 1


def test_exit_code_neutralized_by_default(tmp_path):
    """Fail-soft channel split: a non-zero inner exit is reported as 0 to the
    harness (so it never cancels sibling parallel calls), with the real code
    carried in-band as an [exit N] marker."""
    rc, out, err = _run('exit 7', tmp_path)
    assert rc == 0
    assert '[exit 7]' in out


def test_exit_code_propagates_with_flag(tmp_path):
    """CCH_PROPAGATE_EXIT=1 restores raw exit-code propagation."""
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)
    env['CCH_PROPAGATE_EXIT'] = '1'
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), '--', 'exit 7'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10,
    )
    assert proc.returncode == 7
    assert b'[exit 7]' not in proc.stdout


def test_zero_exit_has_no_marker(tmp_path):
    """A successful command gets no [exit N] noise."""
    rc, out, err = _run('echo ok', tmp_path)
    assert rc == 0
    assert '[exit' not in out


def test_failsoft_sibling_would_survive(tmp_path):
    """Regression proxy for the cascade: a benign-non-zero command (grep
    no-match → exit 1) reports 0, so in a real parallel batch it would not
    cancel its siblings. The §5 repro at the harness level."""
    rc, out, err = _run('echo keep; grep zzz_no_such /etc/hostname', tmp_path)
    assert rc == 0
    assert 'keep' in out
    assert '[exit 1]' in out


def test_shell_features_work(tmp_path):
    """Pipes/redirects/&& operate inside bash -c."""
    rc, out, err = _run('echo a; echo b | tr a-z A-Z', tmp_path)
    assert rc == 0
    assert 'a\nB\n' == out


def test_binary_passthrough(tmp_path):
    """Binary stdout above threshold (invalid UTF-8) passes through unchanged."""
    # 0xff is never valid UTF-8 — forces the decode to fail and the
    # wrapper to fall back to raw passthrough.
    inner = 'python3 -c "import sys; sys.stdout.buffer.write(b\\"\\\\xff\\" * 4096)"'
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)
    env['CCH_CACHE_THRESHOLD'] = '100'
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), '--', inner],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10,
    )
    assert proc.returncode == 0
    assert b'[CCM_CACHED]' not in proc.stdout
    assert proc.stdout == b'\xff' * 4096
    blobs_dir = tmp_path / '.claude' / 'cache' / 'ccm' / 'blobs'
    assert not blobs_dir.exists() or not list(blobs_dir.iterdir())


def test_missing_dashdash_errors(tmp_path):
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), 'echo', 'hi'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=5,
    )
    assert proc.returncode == 2
    assert b'usage' in proc.stderr.lower()
