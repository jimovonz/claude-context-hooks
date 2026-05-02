"""Tests for intercept-bash.py — verifies command rewrite via updatedInput."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / 'hooks' / 'intercept-bash.py'


def test_rewrites_normal_command(run_hook):
    rc, out, err = run_hook(HOOK, {'tool_input': {'command': 'rg foo src/'}})
    assert rc == 0
    assert isinstance(out, dict)
    rewritten = out['hookSpecificOutput']['updatedInput']['command']
    assert 'cache-wrap.py' in rewritten
    assert "'rg foo src/'" in rewritten or 'rg foo src/' in rewritten


def test_preserves_other_tool_input_fields(run_hook):
    payload = {'tool_input': {'command': 'ls', 'timeout': 5000}}
    rc, out, err = run_hook(HOOK, payload)
    updated = out['hookSpecificOutput']['updatedInput']
    assert updated['timeout'] == 5000
    assert 'cache-wrap.py' in updated['command']


def test_passthrough_for_ccm_get(run_hook):
    rc, out, err = run_hook(HOOK, {'tool_input': {'command': 'ccm-get.py b2s:abc --head 5'}})
    assert out == {}


def test_passthrough_for_already_wrapped(run_hook):
    cmd = '~/.claude/hooks/cache-wrap.py -- ls'
    rc, out, err = run_hook(HOOK, {'tool_input': {'command': cmd}})
    assert out == {}


def test_passthrough_for_empty_command(run_hook):
    rc, out, err = run_hook(HOOK, {'tool_input': {'command': ''}})
    assert out == {}


def test_disable_env_var(run_hook):
    rc, out, err = run_hook(
        HOOK, {'tool_input': {'command': 'rg foo'}}, env={'CCH_DISABLE': '1'}
    )
    assert out == {}


def test_handles_invalid_json(run_hook, tmp_path):
    # Send raw bytes by bypassing the helper
    import subprocess, sys
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=b'not json',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == b'{}'


def test_quotes_command_with_special_chars(run_hook):
    payload = {'tool_input': {'command': "echo 'hi $there' && ls -la"}}
    rc, out, err = run_hook(HOOK, payload)
    rewritten = out['hookSpecificOutput']['updatedInput']['command']
    # The inner command should be a single shell-quoted argument so
    # cache-wrap.py receives it intact via sys.argv[2].
    assert 'cache-wrap.py' in rewritten
    assert '--' in rewritten
