"""Tests for intercept-read.py"""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import StringIO

sys.path.insert(0, str(Path(__file__).parent.parent / 'hooks'))
import importlib


def _import_read():
    spec = importlib.util.spec_from_file_location(
        "intercept_read",
        Path(__file__).parent.parent / 'hooks' / 'intercept-read.py'
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


read_mod = _import_read()


# ─── PASSTHROUGH_PATTERNS ────────────────────────────────────

#TAG: [F001]
# Verifies: PASSTHROUGH_PATTERNS matches CLAUDE.md, README.md, JSON, YAML, TOML
@pytest.mark.behavioural
def test_passthrough_patterns_match():
    pat = read_mod.PASSTHROUGH_PATTERNS
    assert pat.search("CLAUDE.md")
    assert pat.search("README.md")
    assert pat.search("package.json")
    assert pat.search("config.yaml")
    assert pat.search("settings.yml")
    assert pat.search("pyproject.toml")
    assert pat.search("yarn.lock")
    assert pat.search(".env")


#TAG: [F002]
# Verifies: PASSTHROUGH_PATTERNS does not match arbitrary Python or JS files
@pytest.mark.behavioural
def test_passthrough_patterns_no_match():
    pat = read_mod.PASSTHROUGH_PATTERNS
    assert not pat.search("main.py")
    assert not pat.search("app.js")
    assert not pat.search("index.html")
    assert not pat.search("data.csv")


# ─── main (non-trivial: 4 tests) ────────────────────────────

#TAG: [F003]
# Verifies: main passes through for non-Read tool
@pytest.mark.behavioural
def test_main_non_read_tool(capsys):
    input_data = {
        "tool_name": "Bash",
        "tool_input": {},
        "transcript_path": "",
        "tool_use_id": "tu_1",
        "session": {"cwd": "/tmp"},
    }
    with patch.dict('os.environ', {}, clear=False), \
         patch.object(read_mod, 'check_passthrough', return_value=None), \
         patch.object(read_mod, 'init_cache', return_value=None), \
         patch('sys.stdin', StringIO(json.dumps(input_data))):
        read_mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out == {}


#TAG: [F004]
# Verifies: main passes through for paginated reads (offset/limit specified)
@pytest.mark.edge
def test_main_paginated_passthrough(capsys):
    input_data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/some/file.py", "offset": 10, "limit": 50},
        "transcript_path": "/tmp/session.jsonl",
        "tool_use_id": "tu_2",
        "session": {"cwd": "/tmp"},
    }
    with patch.object(read_mod, 'check_passthrough', return_value=None), \
         patch.object(read_mod, 'init_cache', return_value=None), \
         patch.object(read_mod, 'allow_if_subagent', return_value=None), \
         patch.object(read_mod, 'log_metric'), \
         patch('sys.stdin', StringIO(json.dumps(input_data))):
        read_mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out == {}


#TAG: [F005]
# Verifies: main blocks access to cache files and provides retrieval hint
@pytest.mark.error
def test_main_blocks_cache_access(capsys):
    input_data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/home/user/.claude/cache/ccm/blobs/ab/cdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab.gz"},
        "transcript_path": "/tmp/session.jsonl",
        "tool_use_id": "tu_3",
        "session": {"cwd": "/tmp"},
    }
    with patch.object(read_mod, 'check_passthrough', return_value=None), \
         patch.object(read_mod, 'init_cache', return_value=None), \
         patch.object(read_mod, 'allow_if_subagent', return_value=None), \
         patch('sys.stdin', StringIO(json.dumps(input_data))):
        read_mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out['decision'] == 'block'
    assert 'ccm-get.py' in out['reason']


#TAG: [F006]
# Verifies: main blocks access to legacy cache paths
@pytest.mark.adversarial
def test_main_blocks_legacy_cache(capsys):
    input_data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/claude-tool-cache/somefile"},
        "transcript_path": "/tmp/session.jsonl",
        "tool_use_id": "tu_4",
        "session": {"cwd": "/tmp"},
    }
    with patch.object(read_mod, 'check_passthrough', return_value=None), \
         patch.object(read_mod, 'init_cache', return_value=None), \
         patch.object(read_mod, 'allow_if_subagent', return_value=None), \
         patch('sys.stdin', StringIO(json.dumps(input_data))):
        read_mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out['decision'] == 'block'
    assert 'Task agent' in out['reason']


# ─── main whitelisting and size threshold ────────────────────

#TAG: [F007]
# Verifies: main passes through for whitelisted file types (JSON)
@pytest.mark.behavioural
def test_main_whitelist_passthrough(capsys, tmp_path):
    json_file = tmp_path / "config.json"
    json_file.write_text("{}" * 10000)  # large JSON file

    input_data = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(json_file)},
        "transcript_path": "/tmp/session.jsonl",
        "tool_use_id": "tu_5",
        "session": {"cwd": str(tmp_path)},
    }
    with patch.object(read_mod, 'check_passthrough', return_value=None), \
         patch.object(read_mod, 'init_cache', return_value=None), \
         patch.object(read_mod, 'allow_if_subagent', return_value=None), \
         patch.object(read_mod, 'log_metric'), \
         patch('sys.stdin', StringIO(json.dumps(input_data))):
        read_mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out == {}


#TAG: [F008]
# Verifies: main passes through for small files under threshold
@pytest.mark.behavioural
def test_main_small_file_passthrough(capsys, tmp_path):
    small_file = tmp_path / "small.py"
    small_file.write_text("x = 1\n")

    input_data = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(small_file)},
        "transcript_path": "/tmp/session.jsonl",
        "tool_use_id": "tu_6",
        "session": {"cwd": str(tmp_path)},
    }
    with patch.object(read_mod, 'check_passthrough', return_value=None), \
         patch.object(read_mod, 'init_cache', return_value=None), \
         patch.object(read_mod, 'allow_if_subagent', return_value=None), \
         patch.object(read_mod, 'log_metric'), \
         patch('sys.stdin', StringIO(json.dumps(input_data))):
        read_mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out == {}
