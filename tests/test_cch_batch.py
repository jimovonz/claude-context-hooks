"""End-to-end tests for cch-batch.py — runs it as a subprocess with HOME
redirected to tmp_path so any ccm cache lands there. Commands are fed on
stdin (one per line)."""
import os
import subprocess
import sys
import time
from pathlib import Path

BATCH = Path(__file__).resolve().parent.parent / 'hooks' / 'cch-batch.py'


def _run(stdin: str, tmp_path: Path, *args, threshold: int | None = None):
    env = os.environ.copy()
    env['HOME'] = str(tmp_path)
    if threshold is not None:
        env['CCH_CACHE_THRESHOLD'] = str(threshold)
    proc = subprocess.run(
        [sys.executable, str(BATCH), *args],
        input=stdin.encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


def test_all_commands_run_and_labelled(tmp_path):
    rc, out, err = _run('echo aaa\necho bbb\necho ccc\n', tmp_path)
    assert rc == 0
    assert 'aaa' in out and 'bbb' in out and 'ccc' in out
    assert '===[ cch-batch 1/3 ]===' in out
    assert '===[ cch-batch 3/3 ]===' in out


def test_benign_nonzero_does_not_cancel_siblings(tmp_path):
    """The whole point: a non-zero command must not drop its neighbours.
    cch-batch always exits 0, and every command's output is present."""
    rc, out, err = _run('echo keep1\ngrep zzz_nomatch /etc/hostname\necho keep2\n', tmp_path)
    assert rc == 0
    assert 'keep1' in out
    assert 'keep2' in out
    assert '[exit 1]' in out  # the grep miss is surfaced, not hidden


def test_batch_always_exits_zero(tmp_path):
    """Even if every command fails, cch-batch exits 0 (one tool call,
    nothing to cancel)."""
    rc, out, err = _run('false\nfalse\nfalse\n', tmp_path)
    assert rc == 0
    assert out.count('[exit 1]') == 3


def test_blank_and_comment_lines_skipped(tmp_path):
    rc, out, err = _run('# a comment\n\necho only\n   \n', tmp_path)
    assert rc == 0
    assert 'only' in out
    assert '1/1' in out  # exactly one command ran


def test_empty_stdin_is_noop(tmp_path):
    rc, out, err = _run('\n# just a comment\n', tmp_path)
    assert rc == 0
    assert 'no commands' in err


def test_input_order_preserved(tmp_path):
    """Output blocks appear in input order even though commands run
    concurrently (sleeps reversed so completion order != input order)."""
    rc, out, err = _run('sleep 0.3; echo slow\necho fast\n', tmp_path)
    assert rc == 0
    i_slow = out.index('1/2')
    i_fast = out.index('2/2')
    assert i_slow < i_fast


def test_commands_run_concurrently(tmp_path):
    """3x sleep 0.5 should finish in well under the 1.5s serial total."""
    start = time.monotonic()
    rc, out, err = _run('sleep 0.5\nsleep 0.5\nsleep 0.5\n', tmp_path, '--jobs', '3')
    elapsed = time.monotonic() - start
    assert rc == 0
    assert elapsed < 1.2  # concurrent ~0.5s + overhead, not ~1.5s serial


def test_large_output_cached_per_command(tmp_path):
    """A command exceeding the cache threshold gets its own [CCM_CACHED]
    stub; a small sibling stays inline."""
    rc, out, err = _run('seq 1 2000\necho tiny\n', tmp_path, threshold=1000)
    assert rc == 0
    assert '[CCM_CACHED]' in out
    assert 'Retrieve: ccm-get.py' in out
    assert 'tiny' in out


def test_no_cache_wrap_mode(tmp_path):
    """--no-cache-wrap runs via bash -c directly; non-zero still marked."""
    rc, out, err = _run('echo direct\nfalse\n', tmp_path, '--no-cache-wrap')
    assert rc == 0
    assert 'direct' in out
    assert '[exit 1]' in out


def test_failing_command_does_not_sink_batch(tmp_path):
    """A command that errors hard still yields a block; others unaffected."""
    rc, out, err = _run('echo before\nthis_command_does_not_exist_xyz\necho after\n', tmp_path)
    assert rc == 0
    assert 'before' in out
    assert 'after' in out
