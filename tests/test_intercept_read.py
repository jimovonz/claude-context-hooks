"""Tests for intercept-read.py — multimodal-only allowlist, no two-strike."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / 'hooks' / 'intercept-read.py'


def _payload(file_path: str, session: str = 'sess-A'):
    return {
        'tool_input': {'file_path': file_path},
        'transcript_path': f'/tmp/projects/foo/{session}.jsonl',
    }


@pytest.mark.parametrize('path', [
    '/x/foo.png', '/x/foo.PNG', '/x/foo.jpg', '/x/foo.jpeg',
    '/x/foo.gif', '/x/foo.webp', '/x/foo.pdf', '/x/foo.ipynb',
    '/x/foo.svg', '/x/foo.bmp',
])
def test_multimodal_allowed(run_hook, path):
    rc, out, err = run_hook(HOOK, _payload(path))
    assert out == {}


@pytest.mark.parametrize('path', [
    '/x/foo.py', '/x/foo.md', '/x/foo.ts', '/x/foo.json',
    '/x/foo.yaml', '/x/foo', '/x/CMakeLists.txt',
])
def test_text_file_denied(run_hook, path):
    rc, out, err = run_hook(HOOK, _payload(path))
    assert isinstance(out, dict)
    deny = out['hookSpecificOutput']
    assert deny['hookEventName'] == 'PreToolUse'
    assert deny['permissionDecision'] == 'deny'
    reason = deny['permissionDecisionReason']
    assert path in reason
    assert 'cat' in reason
    assert 'cch-edit' in reason


def test_no_two_strike(run_hook):
    """Repeated Reads of the same path stay denied — no two-strike escape."""
    payload = _payload('/x/foo.py')
    for _ in range(3):
        rc, out, err = run_hook(HOOK, payload)
        assert out['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_no_file_path_allowed(run_hook):
    """Hook does not deny when file_path is absent — defensive default."""
    rc, out, err = run_hook(HOOK, {'tool_input': {}})
    assert out == {}


def test_disable_env_var_passes_through(run_hook):
    rc, out, err = run_hook(HOOK, _payload('/x/foo.py'), env={'CCH_DISABLE': '1'})
    assert out == {}


def test_malformed_stdin_passes_through(run_hook, tmp_path):
    """Hook is fail-open on JSON parse error so a buggy invocation does
    not block all Reads."""
    import json, os, subprocess, sys
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=b'not-json',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, 'HOME': str(tmp_path)},
        timeout=10,
    )
    out = proc.stdout.decode().strip()
    assert out in ('{}', '')
