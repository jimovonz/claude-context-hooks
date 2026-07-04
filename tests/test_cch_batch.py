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


HOOKS = Path(__file__).resolve().parent.parent / 'hooks'


def test_same_file_edits_serialized_no_lost_updates(tmp_path):
    """Regression for the sibnb booking-widget corruption: N cch-edit calls
    to ONE file in a single batch must all land (previously they raced on
    the shared .cch-tmp staging file and lost updates)."""
    target = tmp_path / 'code.txt'
    target.write_text(''.join(f'line-{i} original\n' for i in range(6)))
    edit = HOOKS / 'cch-edit.py'
    stdin = ''.join(
        f"python3 {edit} {target} 'line-{i} original' 'line-{i} EDITED'\n"
        for i in range(6)
    )
    rc, out, err = _run(stdin, tmp_path)
    assert rc == 0
    content = target.read_text()
    for i in range(6):
        assert f'line-{i} EDITED' in content, f'edit {i} was lost:\n{out}'
    assert 'original' not in content
    assert 'same-file guard' in out


def test_same_file_writes_last_wins_deterministically(tmp_path):
    target = tmp_path / 'w.txt'
    write = HOOKS / 'cch-write.py'
    stdin = (
        f"echo FIRST | python3 {write} {target}\n"
        f"echo SECOND | python3 {write} {target}\n"
    )
    rc, out, err = _run(stdin, tmp_path)
    assert rc == 0
    assert target.read_text().strip() == 'SECOND'


def test_different_files_stay_parallel(tmp_path):
    """The guard must not serialize edits to DIFFERENT files."""
    a, b = tmp_path / 'a.txt', tmp_path / 'b.txt'
    a.write_text('aaa\n'); b.write_text('bbb\n')
    edit = HOOKS / 'cch-edit.py'
    stdin = (
        f"python3 {edit} {a} aaa AAA\n"
        f"python3 {edit} {b} bbb BBB\n"
    )
    rc, out, err = _run(stdin, tmp_path)
    assert rc == 0
    assert a.read_text().strip() == 'AAA'
    assert b.read_text().strip() == 'BBB'
    assert 'same-file guard' not in out


def test_guard_ignores_non_edit_commands_mentioning_same_path(tmp_path):
    """Plain reads of one file are not writers — no serialization marker."""
    f = tmp_path / 'r.txt'
    f.write_text('hello\n')
    rc, out, err = _run(f'cat {f}\ncat {f}\n', tmp_path)
    assert rc == 0
    assert 'same-file guard' not in out
