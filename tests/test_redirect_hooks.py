"""Smoke tests for the three deny-with-redirect hooks."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / 'hooks'


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
    assert expected_cmd in deny['permissionDecisionReason']


@pytest.mark.parametrize('hook_name', [
    'intercept-grep.py', 'intercept-glob.py', 'intercept-webfetch.py',
    'intercept-edit.py', 'intercept-write.py', 'intercept-notebookedit.py',
])
def test_disable_env_var_passes_through(run_hook, hook_name):
    rc, out, err = run_hook(HOOKS / hook_name, {'tool_input': {}}, env={'CCH_DISABLE': '1'})
    assert out == {}
