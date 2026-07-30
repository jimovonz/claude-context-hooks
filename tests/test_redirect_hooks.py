"""Smoke tests for the deny-with-redirect hooks."""

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / 'hooks'


def _expected(hook_name: str, ideal: str) -> str:
    """Grep/Glob suggest rg/fd when installed, else fall back to grep/find.
    The assertion must track whichever binary is actually on PATH so the
    suite passes on machines with or without ripgrep/fd."""
    if hook_name == 'intercept-grep.py' and not shutil.which('rg'):
        return 'grep'
    if hook_name == 'intercept-glob.py' and not (shutil.which('fd') or shutil.which('fdfind')):
        return 'find'
    return ideal


@pytest.mark.parametrize('hook_name,payload,expected_cmd', [
    ('intercept-grep.py',         {'pattern': 'x'},                       'rg'),
    ('intercept-glob.py',         {'pattern': '*.py'},                    'fd'),
    ('intercept-webfetch.py',     {'url': 'https://example.com'},         'curl'),
    ('intercept-edit.py',         {'file_path': '/x/foo.py'},             'cch-edit'),
    ('intercept-write.py',        {'file_path': '/x/foo.py'},             'cch-write'),
    ('intercept-notebookedit.py', {'notebook_path': '/x/foo.ipynb'},      'cch-edit'),
])
def test_denies_and_names_replacement(run_hook, hook_name, payload, expected_cmd):
    rc, out, err = run_hook(HOOKS / hook_name, {'tool_input': payload})
    assert isinstance(out, dict)
    deny = out['hookSpecificOutput']
    assert deny['hookEventName'] == 'PreToolUse'
    assert deny['permissionDecision'] == 'deny'
    assert _expected(hook_name, expected_cmd) in deny['permissionDecisionReason']


@pytest.mark.parametrize('hook_name,payload,fallback_cmd', [
    ('intercept-grep.py', {'pattern': 'x'},    'grep -rn'),
    ('intercept-glob.py', {'pattern': '*.py'}, 'find PATH'),
])
def test_falls_back_when_tool_absent(run_hook, tmp_path, hook_name, payload, fallback_cmd):
    """With neither rg nor fd on PATH the redirect must point at a POSIX
    tool that exists, so the suggestion never dead-ends (the original bug)."""
    empty = tmp_path / 'emptybin'
    empty.mkdir()
    rc, out, err = run_hook(HOOKS / hook_name, {'tool_input': payload},
                            env={'PATH': str(empty)})
    reason = out['hookSpecificOutput']['permissionDecisionReason']
    assert fallback_cmd in reason
    assert 'rg -n' not in reason
    assert ' fd ' not in reason


@pytest.mark.parametrize('hook_name', [
    'intercept-grep.py', 'intercept-glob.py', 'intercept-webfetch.py',
    'intercept-edit.py', 'intercept-write.py', 'intercept-notebookedit.py',
])
def test_disable_env_var_passes_through(run_hook, hook_name):
    rc, out, err = run_hook(HOOKS / hook_name, {'tool_input': {}}, env={'CCH_DISABLE': '1'})
    assert out == {}
