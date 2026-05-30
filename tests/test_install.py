"""Tests for install.py — settings merge, idempotence, --remove."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / 'install.py'


def _run(args, home: Path):
    env = os.environ.copy()
    env['HOME'] = str(home)
    proc = subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=15,
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


def test_install_creates_symlinks(tmp_path):
    rc, out, err = _run([], tmp_path)
    assert rc == 0
    hooks = tmp_path / '.claude' / 'hooks'
    assert (hooks / 'intercept-bash.py').is_symlink()
    assert (hooks / 'intercept-read.py').is_symlink()
    assert (hooks / 'cache-wrap.py').is_symlink()
    assert (hooks / 'lib' / 'ccm_cache.py').is_symlink()


def test_install_registers_hooks(tmp_path):
    _run([], tmp_path)
    settings = json.loads((tmp_path / '.claude' / 'settings.json').read_text())
    matchers = [
        e.get('matcher')
        for e in settings['hooks']['PreToolUse']
        if any('intercept-' in h.get('command', '') for h in e.get('hooks', []))
    ]
    assert set(matchers) == {'Bash', 'Read', 'Grep', 'Glob', 'WebFetch', 'Edit', 'Write', 'NotebookEdit', 'Agent'}


def test_install_appends_after_existing_bash_hook(tmp_path):
    """Simulate an existing RTK PreToolUse:Bash entry — we must append, not replace."""
    settings_path = tmp_path / '.claude' / 'settings.json'
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        'hooks': {
            'PreToolUse': [{
                'matcher': 'Bash',
                'hooks': [{'type': 'command', 'command': '/usr/local/bin/rtk-rewrite.sh'}],
            }],
        }
    }))
    _run([], tmp_path)
    settings = json.loads(settings_path.read_text())
    pretool = settings['hooks']['PreToolUse']
    bash_entries = [e for e in pretool if e.get('matcher') == 'Bash']
    assert len(bash_entries) == 2
    # RTK is first, ours is second
    assert 'rtk-rewrite' in bash_entries[0]['hooks'][0]['command']
    assert 'intercept-bash' in bash_entries[1]['hooks'][0]['command']


def test_install_idempotent(tmp_path):
    _run([], tmp_path)
    _run([], tmp_path)
    settings = json.loads((tmp_path / '.claude' / 'settings.json').read_text())
    bash_entries = [
        e for e in settings['hooks']['PreToolUse']
        if e.get('matcher') == 'Bash'
        and any('intercept-bash' in h.get('command', '') for h in e.get('hooks', []))
    ]
    assert len(bash_entries) == 1


def test_remove_undoes_install(tmp_path):
    _run([], tmp_path)
    _run(['--remove'], tmp_path)
    hooks = tmp_path / '.claude' / 'hooks'
    assert not (hooks / 'intercept-bash.py').exists()
    settings = json.loads((tmp_path / '.claude' / 'settings.json').read_text())
    pretool = settings.get('hooks', {}).get('PreToolUse', [])
    for entry in pretool:
        for h in entry.get('hooks', []):
            assert 'intercept-' not in h.get('command', '')
            assert 'cache-wrap' not in h.get('command', '')


def test_remove_preserves_other_bash_hooks(tmp_path):
    settings_path = tmp_path / '.claude' / 'settings.json'
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        'hooks': {
            'PreToolUse': [{
                'matcher': 'Bash',
                'hooks': [{'type': 'command', 'command': '/usr/local/bin/rtk-rewrite.sh'}],
            }],
        }
    }))
    _run([], tmp_path)
    _run(['--remove'], tmp_path)
    settings = json.loads(settings_path.read_text())
    pretool = settings['hooks']['PreToolUse']
    rtk_present = any(
        'rtk-rewrite' in h.get('command', '')
        for entry in pretool for h in entry.get('hooks', [])
    )
    assert rtk_present


def test_install_creates_claude_md_with_snippet(tmp_path):
    _run([], tmp_path)
    claude_md = tmp_path / '.claude' / 'CLAUDE.md'
    assert claude_md.exists()
    body = claude_md.read_text()
    assert 'BEGIN claude-context-hooks routing policy' in body
    assert 'END claude-context-hooks routing policy' in body
    assert 'Tool routing' in body  # actual snippet content


def test_install_appends_to_existing_claude_md_preserving_user_content(tmp_path):
    claude_md = tmp_path / '.claude' / 'CLAUDE.md'
    claude_md.parent.mkdir(parents=True)
    user_text = '# My personal CLAUDE.md\n\nSome custom instruction here.\n'
    claude_md.write_text(user_text)
    _run([], tmp_path)
    body = claude_md.read_text()
    assert user_text.rstrip() in body
    assert 'BEGIN claude-context-hooks routing policy' in body


def test_install_idempotent_on_claude_md(tmp_path):
    _run([], tmp_path)
    first = (tmp_path / '.claude' / 'CLAUDE.md').read_text()
    _run([], tmp_path)
    second = (tmp_path / '.claude' / 'CLAUDE.md').read_text()
    # Should NOT duplicate the block
    assert second.count('BEGIN claude-context-hooks routing policy') == 1
    # Content stable across re-installs
    assert first == second


def test_install_no_instructions_flag_skips_claude_md(tmp_path):
    _run(['--no-instructions'], tmp_path)
    claude_md = tmp_path / '.claude' / 'CLAUDE.md'
    assert not claude_md.exists()


def test_remove_strips_claude_md_block_preserving_user_content(tmp_path):
    claude_md = tmp_path / '.claude' / 'CLAUDE.md'
    claude_md.parent.mkdir(parents=True)
    user_text = '# Personal\n\nMy stuff.\n'
    claude_md.write_text(user_text)
    _run([], tmp_path)
    _run(['--remove'], tmp_path)
    body = claude_md.read_text()
    assert 'BEGIN claude-context-hooks routing policy' not in body
    assert 'My stuff.' in body


def test_remove_deletes_claude_md_when_only_our_block(tmp_path):
    _run([], tmp_path)
    _run(['--remove'], tmp_path)
    assert not (tmp_path / '.claude' / 'CLAUDE.md').exists()


def test_install_creates_bin_symlinks(tmp_path):
    rc, out, err = _run([], tmp_path)
    assert rc == 0
    bin_dst = tmp_path / '.local' / 'bin'
    for name in ('cch-edit.py', 'cch-write.py', 'ccm-get.py'):
        link = bin_dst / name
        assert link.is_symlink(), f'{name} missing from {bin_dst}'
        assert link.resolve() == (REPO_ROOT / 'hooks' / name).resolve()


def test_install_bin_symlinks_idempotent(tmp_path):
    _run([], tmp_path)
    _run([], tmp_path)
    bin_dst = tmp_path / '.local' / 'bin'
    # Still exactly one of each, still pointing into the repo
    for name in ('cch-edit.py', 'cch-write.py', 'ccm-get.py'):
        link = bin_dst / name
        assert link.is_symlink()
        assert link.resolve() == (REPO_ROOT / 'hooks' / name).resolve()


def test_install_preserves_existing_bin_collision(tmp_path):
    """If the user has a script with the same name, do NOT clobber it."""
    bin_dst = tmp_path / '.local' / 'bin'
    bin_dst.mkdir(parents=True)
    user_script = bin_dst / 'cch-edit.py'
    user_script.write_text('#!/bin/sh\necho user-owned\n')
    user_script.chmod(0o755)
    rc, out, err = _run([], tmp_path)
    assert rc == 0
    # Still a regular file, not our symlink
    assert user_script.is_file()
    assert not user_script.is_symlink()
    assert 'user-owned' in user_script.read_text()


def test_remove_strips_bin_symlinks(tmp_path):
    _run([], tmp_path)
    _run(['--remove'], tmp_path)
    bin_dst = tmp_path / '.local' / 'bin'
    for name in ('cch-edit.py', 'cch-write.py', 'ccm-get.py'):
        assert not (bin_dst / name).exists(), f'{name} not removed'


def test_remove_preserves_user_script_in_bin(tmp_path):
    """--remove must NOT delete a user-owned file with a colliding name."""
    bin_dst = tmp_path / '.local' / 'bin'
    bin_dst.mkdir(parents=True)
    user_script = bin_dst / 'cch-edit.py'
    user_script.write_text('#!/bin/sh\necho user-owned\n')
    user_script.chmod(0o755)
    _run([], tmp_path)
    _run(['--remove'], tmp_path)
    assert user_script.is_file()
    assert 'user-owned' in user_script.read_text()


def test_check_only(tmp_path):
    rc, out, err = _run(['--check'], tmp_path)
    assert rc == 0
    assert 'python_310' in out
    assert 'rtk_on_path' in out
    # No installation
    assert not (tmp_path / '.claude' / 'hooks').exists()
