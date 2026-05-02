"""Smoke tests for the three deny-with-redirect hooks."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / 'hooks'


@pytest.mark.parametrize('hook_name,expected_cmd', [
    ('intercept-grep.py', 'rg'),
    ('intercept-glob.py', 'fd'),
    ('intercept-webfetch.py', 'curl'),
])
def test_denies_and_names_replacement(run_hook, hook_name, expected_cmd):
    rc, out, err = run_hook(HOOKS / hook_name, {'tool_input': {'pattern': 'x'}})
    assert isinstance(out, dict)
    deny = out['hookSpecificOutput']
    assert deny['hookEventName'] == 'PreToolUse'
    assert deny['permissionDecision'] == 'deny'
    assert expected_cmd in deny['permissionDecisionReason']
    assert 'Bash' in deny['permissionDecisionReason']


@pytest.mark.parametrize('hook_name', [
    'intercept-grep.py', 'intercept-glob.py', 'intercept-webfetch.py',
])
def test_disable_env_var_passes_through(run_hook, hook_name):
    rc, out, err = run_hook(HOOKS / hook_name, {'tool_input': {}}, env={'CCH_DISABLE': '1'})
    assert out == {}
