"""Tests for intercept-bash.py"""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
import importlib


def _import_bash():
    spec = importlib.util.spec_from_file_location(
        "intercept_bash",
        Path(__file__).parent.parent / 'hooks' / 'intercept-bash.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bash_mod = _import_bash()


# ─── is_ccm_script ───────────────────────────────────────────

#TAG: [H001]
# Verifies: is_ccm_script returns True for known CCM management commands
@pytest.mark.behavioural
def test_is_ccm_script_matches():
    assert bash_mod.is_ccm_script("~/.claude/hooks/ccm-get.py sha256:abc --head 10") is True
    assert bash_mod.is_ccm_script("python3 context-monitor.py") is True
    assert bash_mod.is_ccm_script("/home/user/.claude/hooks/intercept-bash.py") is True


#TAG: [H002]
# Verifies: is_ccm_script returns False for regular commands
@pytest.mark.behavioural
def test_is_ccm_script_no_match():
    assert bash_mod.is_ccm_script("ls -la") is False
    assert bash_mod.is_ccm_script("python3 my_script.py") is False
    assert bash_mod.is_ccm_script("git status") is False


# ─── is_obviously_small ──────────────────────────────────────

#TAG: [H003]
# Verifies: is_obviously_small returns True for simple known-small commands
@pytest.mark.behavioural
def test_is_obviously_small_matches():
    assert bash_mod.is_obviously_small("ls") is True
    assert bash_mod.is_obviously_small("pwd") is True
    assert bash_mod.is_obviously_small("echo hello world") is True
    assert bash_mod.is_obviously_small("git status") is True
    assert bash_mod.is_obviously_small("mkdir -p /tmp/test") is True
    assert bash_mod.is_obviously_small("head -5 file.txt") is True
    assert bash_mod.is_obviously_small("MY_VAR=value") is True


#TAG: [H004]
# Verifies: is_obviously_small returns False for compound and unknown commands
@pytest.mark.behavioural
def test_is_obviously_small_no_match():
    assert bash_mod.is_obviously_small("cat file.txt && grep error") is False
    assert bash_mod.is_obviously_small("find / -name '*.py'") is False
    assert bash_mod.is_obviously_small("npm install") is False
    assert bash_mod.is_obviously_small("cat large_file | sort") is False


# ─── is_obviously_interactive ────────────────────────────────

#TAG: [H005]
# Verifies: is_obviously_interactive returns True for TTY-based commands
@pytest.mark.behavioural
def test_is_obviously_interactive_matches():
    assert bash_mod.is_obviously_interactive("vim file.py") is True
    assert bash_mod.is_obviously_interactive("nano config.txt") is True
    assert bash_mod.is_obviously_interactive("ssh user@host") is True
    assert bash_mod.is_obviously_interactive("python3") is True
    assert bash_mod.is_obviously_interactive("less output.log") is True


#TAG: [H006]
# Verifies: is_obviously_interactive returns True for commands with -i flag
@pytest.mark.behavioural
def test_is_obviously_interactive_i_flag():
    assert bash_mod.is_obviously_interactive("grep -i pattern file") is True
    assert bash_mod.is_obviously_interactive("sed -i 's/a/b/' file") is True


# ─── classify_unknown_command ────────────────────────────────

#TAG: [H007]
# Verifies: classify_unknown_command returns default non-interactive classification on failure
@pytest.mark.behavioural
def test_classify_unknown_command_default():
    with patch.object(bash_mod, 'get_command_classification', return_value=None):
        result = bash_mod.classify_unknown_command("some_custom_cmd")
        assert result['interactive'] == 0
        assert result['large_output'] == 0
