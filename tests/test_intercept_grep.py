"""Tests for intercept-grep.py"""

import json
import subprocess
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
import importlib


def _import_grep():
    spec = importlib.util.spec_from_file_location(
        "intercept_grep",
        Path(__file__).parent.parent / 'hooks' / 'intercept-grep.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grep_mod = _import_grep()


# ─── find_ripgrep ────────────────────────────────────────────

#TAG: [D001]
# Verifies: find_ripgrep returns 'rg' when system ripgrep is available
@pytest.mark.behavioural
def test_find_ripgrep_system(tmp_path):
    with patch('shutil.which', side_effect=lambda x: '/usr/bin/rg' if x == 'rg' else None):
        with patch.object(Path, 'is_dir', return_value=False):
            result = grep_mod.find_ripgrep()
            assert result == 'rg'


#TAG: [D002]
# Verifies: find_ripgrep returns None when no ripgrep found anywhere
@pytest.mark.behavioural
def test_find_ripgrep_not_found():
    with patch('shutil.which', return_value=None):
        with patch.object(Path, 'is_dir', return_value=False):
            result = grep_mod.find_ripgrep()
            assert result is None


# ─── run_grep (non-trivial: 4 tests) ─────────────────────────

#TAG: [D003]
# Verifies: run_grep returns ripgrep output and exit code for valid pattern
@pytest.mark.behavioural
def test_run_grep_basic():
    mock_result = MagicMock()
    mock_result.stdout = "file1.py\nfile2.py\n"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch.object(grep_mod, 'find_ripgrep', return_value='rg'):
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            input_data = {
                'tool_input': {
                    'pattern': 'def main',
                    'output_mode': 'files_with_matches',
                }
            }
            output, code = grep_mod.run_grep(input_data, '/src')
            assert output == "file1.py\nfile2.py\n"
            assert code == 0
            # Verify -l flag was passed for files_with_matches
            cmd_args = mock_run.call_args[0][0]
            assert '-l' in cmd_args


#TAG: [D004]
# Verifies: run_grep applies offset and head_limit to output lines
@pytest.mark.edge
def test_run_grep_offset_head_limit():
    mock_result = MagicMock()
    mock_result.stdout = "line0\nline1\nline2\nline3\nline4\n"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch.object(grep_mod, 'find_ripgrep', return_value='rg'):
        with patch('subprocess.run', return_value=mock_result):
            input_data = {
                'tool_input': {
                    'pattern': '.',
                    'output_mode': 'content',
                    'offset': 1,
                    'head_limit': 2,
                }
            }
            output, code = grep_mod.run_grep(input_data, '/src')
            lines = output.strip().split('\n')
            assert lines[0] == 'line1'
            assert lines[1] == 'line2'
            assert len(lines) == 2


#TAG: [D005]
# Verifies: run_grep returns error message when ripgrep not found
@pytest.mark.error
def test_run_grep_no_ripgrep():
    with patch.object(grep_mod, 'find_ripgrep', return_value=None):
        output, code = grep_mod.run_grep({}, '/src')
        assert "not found" in output.lower()
        assert code == 1


#TAG: [D006]
# Verifies: run_grep returns timeout message when subprocess exceeds timeout
@pytest.mark.adversarial
def test_run_grep_timeout():
    with patch.object(grep_mod, 'find_ripgrep', return_value='rg'):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('rg', 60)):
            input_data = {'tool_input': {'pattern': '.'}}
            output, code = grep_mod.run_grep(input_data, '/src')
            assert "timed out" in output.lower()
            assert code == 124


# ─── run_grep options ────────────────────────────────────────

#TAG: [D007]
# Verifies: run_grep passes multiline and case-insensitive flags correctly
@pytest.mark.behavioural
def test_run_grep_options():
    mock_result = MagicMock()
    mock_result.stdout = "match\n"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch.object(grep_mod, 'find_ripgrep', return_value='rg'):
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            input_data = {
                'tool_input': {
                    'pattern': 'test',
                    'output_mode': 'content',
                    '-i': True,
                    'multiline': True,
                    'glob': '*.py',
                    'type': 'py',
                    '-A': 2,
                    '-B': 1,
                }
            }
            grep_mod.run_grep(input_data, '/src')
            cmd = mock_run.call_args[0][0]
            assert '-i' in cmd
            assert '-U' in cmd
            assert '--multiline-dotall' in cmd
            assert '--glob' in cmd
            assert '*.py' in cmd
            assert '--type' in cmd
            assert '-A' in cmd
            assert '-B' in cmd
