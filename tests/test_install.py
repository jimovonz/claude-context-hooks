"""Tests for install.py"""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import install


# ─── merge_settings (non-trivial: 4 tests) ──────────────────

#TAG: [J001]
# Verifies: merge_settings adds hook entries to empty settings.json
@pytest.mark.behavioural
def test_merge_settings_empty(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    with patch.object(install, 'SETTINGS_FILE', settings_file):
        install.merge_settings()
    result = json.loads(settings_file.read_text())
    assert 'hooks' in result
    assert 'PreToolUse' in result['hooks']
    assert len(result['hooks']['PreToolUse']) == 5  # Bash, Glob, Grep, Read, WebFetch
    assert 'UserPromptSubmit' in result['hooks']
    assert len(result['hooks']['UserPromptSubmit']) == 1


#TAG: [J002]
# Verifies: merge_settings does not duplicate already-registered hooks
@pytest.mark.edge
def test_merge_settings_no_duplicates(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    with patch.object(install, 'SETTINGS_FILE', settings_file):
        install.merge_settings()
        install.merge_settings()  # second call
    result = json.loads(settings_file.read_text())
    assert len(result['hooks']['PreToolUse']) == 5  # still 5, not 10


#TAG: [J003]
# Verifies: merge_settings handles corrupt settings.json by creating fresh settings
@pytest.mark.error
def test_merge_settings_corrupt_file(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("not json{{{")
    with patch.object(install, 'SETTINGS_FILE', settings_file):
        install.merge_settings()
    result = json.loads(settings_file.read_text())
    assert 'hooks' in result


#TAG: [J004]
# Verifies: merge_settings preserves existing non-hook settings
@pytest.mark.adversarial
def test_merge_settings_preserves_existing(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({
        "theme": "dark",
        "hooks": {
            "PreToolUse": [
                {"matcher": "CustomTool", "hooks": [{"type": "command", "command": "my-hook.py"}]}
            ]
        }
    }))
    with patch.object(install, 'SETTINGS_FILE', settings_file):
        install.merge_settings()
    result = json.loads(settings_file.read_text())
    assert result['theme'] == 'dark'
    # Should have custom + our 5
    assert len(result['hooks']['PreToolUse']) == 6
    commands = [
        entry.get('hooks', [{}])[0].get('command', '')
        for entry in result['hooks']['PreToolUse']
    ]
    assert 'my-hook.py' in commands


# ─── remove_hooks ────────────────────────────────────────────

#TAG: [J005]
# Verifies: remove_hooks removes all registered hook entries from settings
@pytest.mark.behavioural
def test_remove_hooks(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    with patch.object(install, 'SETTINGS_FILE', settings_file):
        install.merge_settings()
        install.remove_hooks()
    result = json.loads(settings_file.read_text())
    # All hooks removed, events with empty arrays get deleted
    for event, entries in result.get('hooks', {}).items():
        for entry in entries:
            for hook in entry.get('hooks', []):
                assert '~/.claude/hooks/' not in hook.get('command', '')


# ─── copy_files ──────────────────────────────────────────────

#TAG: [J006]
# Verifies: copy_files creates destination directories and copies hook files
@pytest.mark.behavioural
def test_copy_files(tmp_path):
    hooks_dst = tmp_path / "hooks"
    with patch.object(install, 'HOOKS_DST', hooks_dst), \
         patch.object(install, 'HOOKS_SRC', Path(__file__).parent.parent / 'hooks'), \
         patch('pathlib.Path.home', return_value=tmp_path):
        install.copy_files()
    assert hooks_dst.exists()
    assert (hooks_dst / 'lib').is_dir()
    # At least some files should be copied
    copied_files = list(hooks_dst.glob('*.py'))
    assert len(copied_files) > 0
