"""Tests for intercept-glob.py"""

import json
import subprocess
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
import importlib


def _import_glob():
    spec = importlib.util.spec_from_file_location(
        "intercept_glob",
        Path(__file__).parent.parent / 'hooks' / 'intercept-glob.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


glob_mod = _import_glob()


# ─── run_glob (non-trivial: 4 tests) ────────────────────────

#TAG: [E001]
# Verifies: run_glob uses fd when available and returns file list
@pytest.mark.behavioural
def test_run_glob_with_fd():
    mock_result = MagicMock()
    mock_result.stdout = "/src/a.py\n/src/b.py\n"
    mock_result.returncode = 0

    with patch('shutil.which', return_value='/usr/bin/fd'):
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            output, code = glob_mod.run_glob("**/*.py", "/src")
            assert "/src/a.py" in output
            assert code == 0
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == 'fd'


#TAG: [E002]
# Verifies: run_glob falls back to find when fd is not available
@pytest.mark.edge
def test_run_glob_with_find():
    mock_result = MagicMock()
    mock_result.stdout = "/src/c.py\n/src/a.py\n"
    mock_result.returncode = 0

    with patch('shutil.which', return_value=None):
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            output, code = glob_mod.run_glob("*.py", "/src")
            # find output is sorted
            lines = output.strip().split('\n')
            assert lines == sorted(lines)
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == 'find'


#TAG: [E003]
# Verifies: run_glob returns error on subprocess timeout
@pytest.mark.error
def test_run_glob_timeout():
    with patch('shutil.which', return_value='/usr/bin/fd'):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('fd', 60)):
            output, code = glob_mod.run_glob("**/*", "/")
            assert "timed out" in output.lower()
            assert code == 124


#TAG: [E004]
# Verifies: run_glob handles subprocess exceptions gracefully
@pytest.mark.adversarial
def test_run_glob_exception():
    with patch('shutil.which', return_value='/usr/bin/fd'):
        with patch('subprocess.run', side_effect=OSError("disk error")):
            output, code = glob_mod.run_glob("*.py", "/src")
            assert "disk error" in output
            assert code == 1


#TAG: [E005]
# Verifies: run_glob expands ~ in path argument
@pytest.mark.behavioural
def test_run_glob_tilde_expansion():
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.returncode = 0

    with patch('shutil.which', return_value='/usr/bin/fd'):
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            glob_mod.run_glob("*.py", "~/projects")
            cmd = mock_run.call_args[0][0]
            # Path should be expanded - not start with ~
            path_arg = cmd[-1]
            assert not path_arg.startswith('~')


#TAG: [E006]
# Verifies: run_glob strips leading **/ for fd pattern
@pytest.mark.behavioural
def test_run_glob_fd_pattern_strip():
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.returncode = 0

    with patch('shutil.which', return_value='/usr/bin/fd'):
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            glob_mod.run_glob("**/*.ts", "/src")
            cmd = mock_run.call_args[0][0]
            # fd should get *.ts not **/*.ts
            assert '*.ts' in cmd
            assert '**/*.ts' not in cmd
