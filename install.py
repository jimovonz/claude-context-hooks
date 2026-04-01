#!/usr/bin/env python3
"""
Installer for Claude Code context hooks.

Symlinks hook scripts into ~/.claude/hooks/ and registers them in settings.json.
Does not modify the Claude Code executable in any way.

Usage:
    python install.py           # Install hooks (symlinks)
    python install.py --remove  # Remove hooks and settings registrations
"""

import json
import os
import sys
from pathlib import Path

HOOKS_SRC = Path(__file__).parent / 'hooks'
HOOKS_DST = Path.home() / '.claude' / 'hooks'
SETTINGS_FILE = Path.home() / '.claude' / 'settings.json'

# Files to install
HOOK_FILES = [
    'intercept-bash.py',
    'intercept-glob.py',
    'intercept-grep.py',
    'intercept-read.py',
    'intercept-webfetch.py',
    'context-monitor.py',
    'ccm-get.py',
    'config.py',
    'lib/__init__.py',
    'lib/ccm_cache.py',
    'lib/common.py',
]

# Hook registration for settings.json
HOOK_CONFIG = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-bash.py"}]
            },
            {
                "matcher": "Glob",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-glob.py"}]
            },
            {
                "matcher": "Grep",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-grep.py"}]
            },
            {
                "matcher": "Read",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-read.py"}]
            },
            {
                "matcher": "WebFetch",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-webfetch.py"}]
            },
        ],
        "UserPromptSubmit": [
            {
                "hooks": [{"type": "command", "command": "~/.claude/hooks/context-monitor.py"}]
            },
        ],
    }
}


def install_files():
    """Symlink hook files into ~/.claude/hooks/."""
    HOOKS_DST.mkdir(parents=True, exist_ok=True)
    (HOOKS_DST / 'lib').mkdir(exist_ok=True)

    # Create cache directories
    cache_dir = Path.home() / '.claude' / 'cache' / 'ccm'
    (cache_dir / 'blobs').mkdir(parents=True, exist_ok=True)
    (cache_dir / 'meta').mkdir(parents=True, exist_ok=True)

    installed = 0
    for f in HOOK_FILES:
        src = (HOOKS_SRC / f).resolve()
        dst = HOOKS_DST / f

        if not src.exists():
            print(f"  SKIP {f} (not found)")
            continue

        # Don't overwrite user's config if it's not a symlink back to us
        if f == 'config.py' and dst.exists() and not dst.is_symlink():
            print(f"  KEEP {f} (user config preserved)")
            continue

        # Remove existing file/symlink before creating new symlink
        if dst.exists() or dst.is_symlink():
            dst.unlink()

        os.symlink(src, dst)
        installed += 1
        print(f"  LINK {f} -> {src}")

    print(f"\n  {installed} files symlinked into {HOOKS_DST}")


def merge_settings():
    """Merge hook registration into settings.json."""
    settings = {}
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
        except json.JSONDecodeError:
            print(f"  WARNING: Could not parse {SETTINGS_FILE}, creating new")

    existing_hooks = settings.get('hooks', {})
    new_hooks = HOOK_CONFIG['hooks']

    for event, hook_entries in new_hooks.items():
        if event not in existing_hooks:
            existing_hooks[event] = []

        # Check for duplicates by command path
        existing_commands = set()
        for entry in existing_hooks[event]:
            for hook in entry.get('hooks', []):
                existing_commands.add(hook.get('command', ''))

        for entry in hook_entries:
            cmd = entry.get('hooks', [{}])[0].get('command', '')
            if cmd not in existing_commands:
                existing_hooks[event].append(entry)
                print(f"  ADD {event}: {cmd}")
            else:
                print(f"  SKIP {event}: {cmd} (already registered)")

    settings['hooks'] = existing_hooks
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')
    print(f"\n  Settings written to {SETTINGS_FILE}")


def remove_hooks():
    """Remove hook symlinks and settings registrations."""
    # Remove symlinks
    removed_files = 0
    for f in HOOK_FILES:
        dst = HOOKS_DST / f
        if dst.is_symlink():
            dst.unlink()
            removed_files += 1
            print(f"  UNLINK {f}")
        elif dst.exists():
            print(f"  SKIP {f} (not a symlink — left in place)")

    # Clean up empty lib dir
    lib_dir = HOOKS_DST / 'lib'
    if lib_dir.is_dir() and not any(lib_dir.iterdir()):
        lib_dir.rmdir()
        print(f"  RMDIR lib/")

    if removed_files:
        print(f"\n  {removed_files} symlinks removed from {HOOKS_DST}")

    # Remove settings registrations
    if not SETTINGS_FILE.exists():
        print("\n  No settings.json found")
        return

    settings = json.loads(SETTINGS_FILE.read_text())
    hooks = settings.get('hooks', {})

    our_commands = set()
    for entries in HOOK_CONFIG['hooks'].values():
        for entry in entries:
            for hook in entry.get('hooks', []):
                our_commands.add(hook.get('command', ''))

    removed_reg = 0
    for event in list(hooks.keys()):
        original_len = len(hooks[event])
        hooks[event] = [
            entry for entry in hooks[event]
            if not any(
                hook.get('command', '') in our_commands
                for hook in entry.get('hooks', [])
            )
        ]
        removed_reg += original_len - len(hooks[event])
        if not hooks[event]:
            del hooks[event]

    settings['hooks'] = hooks
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')
    print(f"\n  Removed {removed_reg} hook registrations from {SETTINGS_FILE}")


def check_optional_deps():
    """Check for optional dependencies."""
    print("\nOptional dependencies:")

    try:
        import zstandard
        print("  zstandard: installed (better compression)")
    except ImportError:
        print("  zstandard: not installed (will use gzip)")
        print("    Install: pip install zstandard")

    try:
        import tiktoken
        print("  tiktoken: installed (accurate token counting)")
    except ImportError:
        print("  tiktoken: not installed (will estimate from char count)")
        print("    Install: pip install tiktoken")


def main():
    if '--remove' in sys.argv:
        print("Removing Claude Code context hooks...\n")
        remove_hooks()
        print("\nDone.")
        return

    print("Installing Claude Code context hooks...\n")
    install_files()
    print()
    merge_settings()
    check_optional_deps()
    print("\nDone. Hooks will activate on next Claude Code session.")
    print("Updates: git pull (symlinks update automatically)")


if __name__ == '__main__':
    main()
