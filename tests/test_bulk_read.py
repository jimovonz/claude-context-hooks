"""Tests for intercept-bash.py bulk-read gate."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / 'hooks' / 'intercept-bash.py'


def _assert_denied(out):
    assert out['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert 'cairn-graph' in out['hookSpecificOutput']['permissionDecisionReason']


def _assert_allowed(out):
    """Allowed = either empty dict (passthrough) or updatedInput (cache-wrap)."""
    if out == {}:
        return
    assert 'updatedInput' in out.get('hookSpecificOutput', {})


# --- cat: always deny on code files ---

def test_cat_py_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'cat foo.py'}})
    assert rc == 0
    _assert_denied(out)


def test_cat_ts_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'cat src/index.ts'}})
    assert rc == 0
    _assert_denied(out)


def test_cat_rs_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'cat main.rs'}})
    assert rc == 0
    _assert_denied(out)


def test_cat_with_flags_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'cat -n foo.py'}})
    assert rc == 0
    _assert_denied(out)


def test_rtk_cat_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'rtk cat foo.py'}})
    assert rc == 0
    _assert_denied(out)


def test_cat_noncode_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'cat README.md'}})
    assert rc == 0
    _assert_allowed(out)


def test_cat_json_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'cat package.json'}})
    assert rc == 0
    _assert_allowed(out)


def test_cat_piped_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'cat foo.py | grep def'}})
    assert rc == 0
    _assert_denied(out)


def test_nonbulk_piped_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'rg pattern foo.py | sort'}})
    assert rc == 0
    _assert_allowed(out)


# --- head/tail: deny only with large -n ---

def test_head_large_n_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'head -n 200 foo.py'}})
    assert rc == 0
    _assert_denied(out)


def test_head_small_n_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'head -n 20 foo.py'}})
    assert rc == 0
    _assert_allowed(out)


def test_head_no_n_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'head foo.py'}})
    assert rc == 0
    _assert_allowed(out)


def test_head_exact_threshold_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'head -n 49 foo.py'}})
    assert rc == 0
    _assert_allowed(out)


def test_head_at_threshold_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'head -n 50 foo.py'}})
    assert rc == 0
    _assert_denied(out)


def test_tail_large_n_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'tail -n 100 foo.py'}})
    assert rc == 0
    _assert_denied(out)


def test_tail_no_n_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'tail foo.py'}})
    assert rc == 0
    _assert_allowed(out)


def test_rtk_head_large_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'rtk head -n 200 foo.py'}})
    assert rc == 0
    _assert_denied(out)


def test_head_noncode_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'head -n 200 output.log'}})
    assert rc == 0
    _assert_allowed(out)


def test_head_shorthand_n_denied(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'head -200 foo.py'}})
    assert rc == 0
    _assert_denied(out)


# --- sed: always allowed (line-specific) with optional warning ---

def test_sed_wide_range_allowed_with_warn(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': "sed -n '1,200p' foo.py"}})
    assert rc == 0
    _assert_allowed(out)
    cmd = out['hookSpecificOutput']['updatedInput']['command']
    assert 'echo "[cch:' in cmd


def test_sed_narrow_range_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': "sed -n '10,25p' foo.py"}})
    assert rc == 0
    _assert_allowed(out)


def test_sed_exact_threshold_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': "sed -n '1,50p' foo.py"}})
    assert rc == 0
    _assert_allowed(out)


def test_sed_at_old_threshold_now_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': "sed -n '1,51p' foo.py"}})
    assert rc == 0
    _assert_allowed(out)


def test_sed_noncode_allowed(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': "sed -n '1,500p' config.yaml"}})
    assert rc == 0
    _assert_allowed(out)


def test_sed_double_quotes_allowed_with_warn(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'sed -n "1,200p" foo.py'}})
    assert rc == 0
    _assert_allowed(out)
    cmd = out['hookSpecificOutput']['updatedInput']['command']
    assert 'echo "[cch:' in cmd


def test_sed_under_warn_threshold_no_warning(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': "sed -n '1,80p' foo.py"}})
    assert rc == 0
    _assert_allowed(out)
    cmd = out.get('hookSpecificOutput', {}).get('updatedInput', {}).get('command', '')
    assert '[cch:' not in cmd


def test_sed_at_warn_threshold_warns(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': "sed -n '1,101p' foo.py"}})
    assert rc == 0
    _assert_allowed(out)
    cmd = out['hookSpecificOutput']['updatedInput']['command']
    assert 'echo "[cch:' in cmd


# --- non-read commands pass through ---

def test_rg_not_affected(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'rg -n pattern foo.py'}})
    assert rc == 0
    _assert_allowed(out)


def test_echo_not_affected(run_hook):
    rc, out, _ = run_hook(HOOK, {'tool_input': {'command': 'echo hello'}})
    assert rc == 0
    _assert_allowed(out)
