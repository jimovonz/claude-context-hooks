"""Tests for lib/common.py"""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import StringIO

import lib.common as common


# ─── check_passthrough ───────────────────────────────────────

#TAG: [B001]
# Verifies: check_passthrough exits with {} when env var is set
@pytest.mark.behavioural
def test_check_passthrough_exits(capsys):
    with patch.dict(os.environ, {'CLAUDE_HOOKS_PASSTHROUGH': '1'}):
        with pytest.raises(SystemExit) as exc_info:
            common.check_passthrough()
        assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == '{}'


# ─── get_common_fields ───────────────────────────────────────

#TAG: [B002]
# Verifies: get_common_fields extracts tool, transcript_path, tool_use_id, cwd
@pytest.mark.behavioural
def test_get_common_fields_extracts():
    data = {
        'tool_name': 'Bash',
        'transcript_path': '/path/to/session.jsonl',
        'tool_use_id': 'tu_123',
        'session': {'cwd': '/home/user'},
    }
    tool, tp, tuid, cwd = common.get_common_fields(data)
    assert tool == 'Bash'
    assert tp == '/path/to/session.jsonl'
    assert tuid == 'tu_123'
    assert cwd == '/home/user'


# ─── json_block ──────────────────────────────────────────────

#TAG: [B003]
# Verifies: json_block outputs block decision with reason
@pytest.mark.behavioural
def test_json_block_outputs_block(capsys):
    common.json_block("test reason")
    out = json.loads(capsys.readouterr().out)
    assert out['decision'] == 'block'
    assert out['reason'] == 'test reason'


#TAG: [B004]
# Verifies: json_block prepends 'None - ' when exit_code is 0
@pytest.mark.behavioural
def test_json_block_with_exit_zero(capsys):
    common.json_block("output text", exit_code=0)
    out = json.loads(capsys.readouterr().out)
    assert out['reason'].startswith("None - ")
    assert "output text" in out['reason']


# ─── json_pass ───────────────────────────────────────────────

#TAG: [B005]
# Verifies: json_pass outputs empty JSON object
@pytest.mark.behavioural
def test_json_pass_outputs_empty(capsys):
    common.json_pass()
    assert capsys.readouterr().out.strip() == '{}'


# ─── is_key_seen / mark_key_seen ─────────────────────────────

#TAG: [B006]
# Verifies: is_key_seen returns False for unseen key, True after marking
@pytest.mark.behavioural
def test_is_key_seen_after_mark(tmp_path):
    with patch.object(common, 'SESSION_STATE_DIR', tmp_path):
        tp = str(tmp_path / "session.jsonl")
        assert common.is_key_seen(tp, "key1") is False
        common.mark_key_seen(tp, "key1")
        assert common.is_key_seen(tp, "key1") is True


#TAG: [B007]
# Verifies: mark_key_seen returns True on first call, False on subsequent calls
@pytest.mark.behavioural
def test_mark_key_seen_returns_first_time(tmp_path):
    with patch.object(common, 'SESSION_STATE_DIR', tmp_path):
        tp = str(tmp_path / "session.jsonl")
        assert common.mark_key_seen(tp, "key2") is True
        assert common.mark_key_seen(tp, "key2") is False


# ─── should_show_guidance / mark_guidance_shown ──────────────

#TAG: [B008]
# Verifies: should_show_guidance returns True initially, False after marking
@pytest.mark.behavioural
def test_guidance_shown_lifecycle(tmp_path):
    with patch.object(common, 'SESSION_STATE_DIR', tmp_path):
        tp = str(tmp_path / "session.jsonl")
        assert common.should_show_guidance(tp) is True
        common.mark_guidance_shown(tp)
        assert common.should_show_guidance(tp) is False


#TAG: [B009]
# Verifies: should_show_guidance handles missing state file gracefully
@pytest.mark.behavioural
def test_guidance_shown_no_state(tmp_path):
    with patch.object(common, 'SESSION_STATE_DIR', tmp_path / 'nonexistent'):
        tp = str(tmp_path / "session.jsonl")
        assert common.should_show_guidance(tp) is True


# ─── get_size_category ───────────────────────────────────────

#TAG: [B00A]
# Verifies: get_size_category returns SMALL for content under 25KB
@pytest.mark.behavioural
def test_get_size_category_small():
    cat, guidance, action = common.get_size_category(10000)
    assert cat == "SMALL"
    assert "tokens" in guidance


#TAG: [B00B]
# Verifies: get_size_category returns correct categories for each size range
@pytest.mark.behavioural
def test_get_size_category_ranges():
    assert common.get_size_category(30000)[0] == "MEDIUM"
    assert common.get_size_category(60000)[0] == "LARGE"
    assert common.get_size_category(150000)[0] == "MASSIVE"


# ─── build_retrieval_guidance ────────────────────────────────

#TAG: [B00C]
# Verifies: build_retrieval_guidance includes category and guidance in verbose mode
@pytest.mark.behavioural
def test_build_retrieval_guidance_verbose():
    result = common.build_retrieval_guidance(30000, 100, verbose=True)
    assert "category: MEDIUM" in result
    assert "guidance:" in result


#TAG: [B00D]
# Verifies: build_retrieval_guidance returns minimal format in non-verbose mode
@pytest.mark.behavioural
def test_build_retrieval_guidance_minimal():
    result = common.build_retrieval_guidance(30000, 100, verbose=False)
    assert "category: MEDIUM" in result
    assert "guidance:" not in result


# ─── build_duplicate_stub ────────────────────────────────────

#TAG: [B00E]
# Verifies: build_duplicate_stub truncates long keys and includes marker text
@pytest.mark.behavioural
def test_build_duplicate_stub_format():
    result = common.build_duplicate_stub("sha256:abcdef1234567890abcdef1234567890")
    assert "[CCM:" in result
    assert "see earlier" in result
    assert "..." in result  # truncated


# ─── is_subagent (non-trivial: 4 tests) ─────────────────────

#TAG: [B00F]
# Verifies: is_subagent returns True when tool_use_id found in agent file
@pytest.mark.behavioural
def test_is_subagent_found_in_agent_file(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.touch()
    agent_file = tmp_path / "agent-001.jsonl"
    agent_file.write_text(
        '{"message":{"content":[{"type":"tool_use","id":"tu_abc123"}]}}\n'
    )
    assert common.is_subagent(str(transcript), "tu_abc123") is True


#TAG: [B010]
# Verifies: is_subagent returns False when no agent files exist
@pytest.mark.edge
def test_is_subagent_no_agent_files(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.touch()
    assert common.is_subagent(str(transcript), "tu_xyz") is False


#TAG: [B011]
# Verifies: is_subagent returns False for empty transcript_path
@pytest.mark.error
def test_is_subagent_empty_path():
    assert common.is_subagent("", "tu_123") is False


#TAG: [B012]
# Verifies: is_subagent handles large agent files by only reading tail
@pytest.mark.adversarial
def test_is_subagent_large_file(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.touch()
    agent_file = tmp_path / "agent-002.jsonl"
    # Write 128KB of padding then the target
    padding = "x" * (128 * 1024)
    agent_file.write_text(
        padding + '\n{"message":{"content":[{"type":"tool_use","id":"tu_tail"}]}}\n'
    )
    # Tool use ID is in the last 64KB so it should be found
    assert common.is_subagent(str(transcript), "tu_tail") is True


# ─── is_subagent subagents/ subdirectory ─────────────────────

#TAG: [B013]
# Verifies: is_subagent checks subagents/ subdirectory for agent files
@pytest.mark.behavioural
def test_is_subagent_subagents_dir(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.touch()
    # Create subagents dir matching session stem
    subagents_dir = tmp_path / "session" / "subagents"
    subagents_dir.mkdir(parents=True)
    agent_file = subagents_dir / "agent-sub.jsonl"
    agent_file.write_text('{"message":{"content":[{"type":"tool_use","id":"tu_sub"}]}}\n')
    assert common.is_subagent(str(transcript), "tu_sub") is True


# ─── run_command (non-trivial: 4 tests) ─────────────────────

#TAG: [B014]
# Verifies: run_command returns stdout+stderr and exit code for simple commands
@pytest.mark.behavioural
def test_run_command_basic():
    output, code = common.run_command("echo hello")
    assert "hello" in output
    assert code == 0


#TAG: [B015]
# Verifies: run_command returns empty output and exit 0 for no-op command
@pytest.mark.edge
def test_run_command_no_output():
    output, code = common.run_command("true")
    assert code == 0


#TAG: [B016]
# Verifies: run_command returns exit code 124 on timeout
@pytest.mark.error
def test_run_command_timeout():
    output, code = common.run_command("sleep 60", timeout=1)
    assert code == 124
    assert "timed out" in output.lower()


#TAG: [B017]
# Verifies: run_command handles invalid cwd by falling back to None
@pytest.mark.adversarial
def test_run_command_invalid_cwd():
    output, code = common.run_command("echo ok", cwd="/nonexistent/path/xyz")
    assert code == 0
    assert "ok" in output


# ─── extract_command_pattern (non-trivial: 4 tests) ─────────

#TAG: [B018]
# Verifies: extract_command_pattern returns base command for simple commands
@pytest.mark.behavioural
def test_extract_command_pattern_simple():
    assert common.extract_command_pattern("ssh user@host") == "ssh"
    assert common.extract_command_pattern("curl http://example.com") == "curl"


#TAG: [B019]
# Verifies: extract_command_pattern returns None for generic interpreters
@pytest.mark.edge
def test_extract_command_pattern_generic():
    assert common.extract_command_pattern("python3 script.py") is None
    assert common.extract_command_pattern("node app.js") is None
    assert common.extract_command_pattern("bash -c 'echo hi'") is None


#TAG: [B01A]
# Verifies: extract_command_pattern returns None for empty input
@pytest.mark.error
def test_extract_command_pattern_empty():
    assert common.extract_command_pattern("") is None


#TAG: [B01B]
# Verifies: extract_command_pattern includes subcommand for multi-level commands
@pytest.mark.adversarial
def test_extract_command_pattern_multi_level():
    assert common.extract_command_pattern("gh auth refresh -h github.com") == "gh auth"
    assert common.extract_command_pattern("docker login registry.io") == "docker login"
    assert common.extract_command_pattern("git credential fill") == "git credential"


# ─── is_cached_interactive / is_cached_large_output ──────────

#TAG: [B01C]
# Verifies: is_cached_interactive returns cached value for known patterns
@pytest.mark.behavioural
def test_is_cached_interactive(tmp_path):
    cache = {"ssh": {"interactive": 1, "large_output": 0}}
    with patch.object(common, 'COMMAND_CACHE_FILE', tmp_path / 'cmd-cache.json'):
        (tmp_path / 'cmd-cache.json').write_text(json.dumps(cache))
        assert common.is_cached_interactive("ssh user@host") is True


#TAG: [B01D]
# Verifies: is_cached_large_output returns cached value for known patterns
@pytest.mark.behavioural
def test_is_cached_large_output(tmp_path):
    cache = {"find": {"interactive": 0, "large_output": 1}}
    with patch.object(common, 'COMMAND_CACHE_FILE', tmp_path / 'cmd-cache.json'):
        (tmp_path / 'cmd-cache.json').write_text(json.dumps(cache))
        assert common.is_cached_large_output("find /") is True


# ─── learn_command_classification ────────────────────────────

#TAG: [B01E]
# Verifies: learn_command_classification stores pattern with source 'learned'
@pytest.mark.behavioural
def test_learn_command_classification(tmp_path):
    with patch.object(common, 'COMMAND_CACHE_FILE', tmp_path / 'cmd-cache.json'):
        common.learn_command_classification("curl http://example.com", large_output=True)
        cache = json.loads((tmp_path / 'cmd-cache.json').read_text())
        assert cache['curl']['large_output'] == 1
        assert cache['curl']['source'] == 'learned'
