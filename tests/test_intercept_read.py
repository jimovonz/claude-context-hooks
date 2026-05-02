"""Tests for intercept-read.py — multimodal allowlist + two-strike Edit-intent."""

import time
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


def test_text_file_denied_first_attempt(run_hook):
    rc, out, err = run_hook(HOOK, _payload('/x/foo.py'))
    assert isinstance(out, dict)
    deny = out['hookSpecificOutput']
    assert deny['permissionDecision'] == 'deny'
    assert 'Bash' in deny['permissionDecisionReason']
    assert '/x/foo.py' in deny['permissionDecisionReason']


def test_text_file_allowed_on_retry(run_hook):
    payload = _payload('/x/foo.py')
    rc1, out1, _ = run_hook(HOOK, payload)
    assert out1['hookSpecificOutput']['permissionDecision'] == 'deny'
    # Same path, same session, within window → allow
    rc2, out2, _ = run_hook(HOOK, payload)
    assert out2 == {}


def test_different_sessions_independent(run_hook):
    rc1, out1, _ = run_hook(HOOK, _payload('/x/foo.py', session='sess-1'))
    assert out1['hookSpecificOutput']['permissionDecision'] == 'deny'
    # Different session → first attempt denied separately
    rc2, out2, _ = run_hook(HOOK, _payload('/x/foo.py', session='sess-2'))
    assert out2['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_third_attempt_denied_again(run_hook):
    """After Edit-intent retry consumes the credit, next read of same path
    is denied again — so a re-Read after editing still routes through Bash."""
    payload = _payload('/x/foo.py')
    run_hook(HOOK, payload)              # deny
    rc2, out2, _ = run_hook(HOOK, payload)  # allow (edit-intent)
    assert out2 == {}
    rc3, out3, _ = run_hook(HOOK, payload)  # deny again
    assert out3['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_no_file_path_allowed(run_hook):
    rc, out, err = run_hook(HOOK, {'tool_input': {}})
    assert out == {}


def test_disable_env_var(run_hook):
    rc, out, err = run_hook(HOOK, _payload('/x/foo.py'), env={'CCH_DISABLE': '1'})
    assert out == {}
