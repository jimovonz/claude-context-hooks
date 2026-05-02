#!/usr/bin/env python3
"""
Installer for claude-context-hooks (v2).

Symlinks hook scripts into ~/.claude/hooks/ and registers them in
settings.json. Pre-flight checks for RTK presence (warning, not blocker).

Coexistence rules (see docs/DESIGN.md):
- PreToolUse:Bash: RTK rewrites first, our cache wrapper second.
  We APPEND our entry — never reorder or remove existing entries.
- PreToolUse:{Read,Grep,Glob,WebFetch}: this project owns the matcher.
- UserPromptSubmit / Stop: untouched (Cairn owns).

By default also writes the routing-policy snippet (docs/CLAUDE_MD_SNIPPET.md)
into ~/.claude/CLAUDE.md between sentinel HTML comments — re-installs replace
that block in place; --remove strips it. Pass --no-instructions to skip.

Usage:
    python install.py                       # install hooks + instructions
    python install.py --no-instructions     # install hooks only
    python install.py --remove              # uninstall both
    python install.py --remove --no-instructions  # leave CLAUDE.md alone
    python install.py --check               # pre-flight checks only
"""

import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
HOOKS_SRC = REPO_ROOT / 'hooks'
HOOKS_DST = Path.home() / '.claude' / 'hooks'
BIN_DST = Path.home() / '.local' / 'bin'
SETTINGS_FILE = Path.home() / '.claude' / 'settings.json'
CLAUDE_MD = Path.home() / '.claude' / 'CLAUDE.md'
SNIPPET_FILE = REPO_ROOT / 'docs' / 'CLAUDE_MD_SNIPPET.md'

# Sentinel markers around the auto-managed routing-policy block in
# ~/.claude/CLAUDE.md. Re-installs replace whatever sits between them;
# --remove strips the block entirely. Anything outside is left alone.
INSTRUCTIONS_BEGIN = '<!-- BEGIN claude-context-hooks routing policy -->'
INSTRUCTIONS_END = '<!-- END claude-context-hooks routing policy -->'

HOOK_FILES = [
    'intercept-bash.py',
    'intercept-read.py',
    'intercept-grep.py',
    'intercept-glob.py',
    'intercept-webfetch.py',
    'intercept-edit.py',
    'intercept-write.py',
    'intercept-notebookedit.py',
    'cache-wrap.py',
    'cch-edit.py',
    'cch-write.py',
    'ccm-get.py',
    'lib/__init__.py',
    'lib/ccm_cache.py',
    'lib/event_log.py',
    'cch-gain.py',
]

# Helpers also symlinked into ~/.local/bin/ so the model can invoke
# them bare (`cch-edit.py PATH old new`) without absolute paths.
# These are installed only into BIN_DST; the canonical copies live in
# HOOKS_DST. We never overwrite an existing entry that points elsewhere
# (could be the user's own script with a colliding name).
BIN_FILES = [
    'cch-edit.py',
    'cch-write.py',
    'ccm-get.py',
    'cch-gain.py',
]

# settings.json structure. PreToolUse:Bash is appended (not replacing
# RTK or anything else); the other matchers we own outright.
HOOK_REGISTRATIONS = [
    ('PreToolUse', 'Bash',     '~/.claude/hooks/intercept-bash.py'),
    ('PreToolUse', 'Read',     '~/.claude/hooks/intercept-read.py'),
    ('PreToolUse', 'Grep',     '~/.claude/hooks/intercept-grep.py'),
    ('PreToolUse', 'Glob',     '~/.claude/hooks/intercept-glob.py'),
    ('PreToolUse', 'WebFetch', '~/.claude/hooks/intercept-webfetch.py'),
    ('PreToolUse', 'Edit',     '~/.claude/hooks/intercept-edit.py'),
    ('PreToolUse', 'Write',    '~/.claude/hooks/intercept-write.py'),
    ('PreToolUse', 'NotebookEdit', '~/.claude/hooks/intercept-notebookedit.py'),
]


def preflight() -> dict:
    """Return a dict of check_name -> (ok, message)."""
    checks = {}
    rtk = shutil.which('rtk')
    checks['rtk_on_path'] = (
        rtk is not None,
        f'rtk found at {rtk}' if rtk else
        'rtk not found — install from https://github.com/rtk-ai/rtk for compression'
    )
    py_ok = sys.version_info >= (3, 10)
    checks['python_310'] = (
        py_ok,
        f'python {sys.version.split()[0]}'
    )
    path_dirs = os.environ.get('PATH', '').split(os.pathsep)
    bin_on_path = str(BIN_DST) in path_dirs
    checks['bin_on_path'] = (
        bin_on_path,
        f'{BIN_DST} on PATH — helpers (cch-edit.py, cch-write.py, ccm-get.py) invokable bare'
        if bin_on_path else
        f'{BIN_DST} not on PATH — add it to your shell rc, or invoke helpers via absolute path'
    )
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
            bash_hooks = []
            for entry in settings.get('hooks', {}).get('PreToolUse', []):
                if entry.get('matcher') == 'Bash':
                    for h in entry.get('hooks', []):
                        bash_hooks.append(h.get('command', ''))
            rtk_present = any('rtk' in c.lower() for c in bash_hooks)
            checks['rtk_bash_hook'] = (
                rtk_present,
                'RTK PreToolUse:Bash hook detected — ours will fire after it'
                if rtk_present else
                'No RTK PreToolUse:Bash hook detected — caching still works, '
                'but commands will not be RTK-compressed before measurement'
            )
        except json.JSONDecodeError:
            checks['rtk_bash_hook'] = (False, f'cannot parse {SETTINGS_FILE}')
    else:
        checks['rtk_bash_hook'] = (False, 'no settings.json yet')
    return checks


def print_preflight(checks: dict) -> bool:
    print('Pre-flight checks:')
    all_blocking_ok = True
    for name, (ok, msg) in checks.items():
        marker = 'OK ' if ok else '!! '
        print(f'  {marker}{name}: {msg}')
        if name == 'python_310' and not ok:
            all_blocking_ok = False
    return all_blocking_ok


def install_files() -> int:
    HOOKS_DST.mkdir(parents=True, exist_ok=True)
    (HOOKS_DST / 'lib').mkdir(exist_ok=True)
    (Path.home() / '.claude' / 'cache' / 'ccm' / 'blobs').mkdir(parents=True, exist_ok=True)
    (Path.home() / '.claude' / 'cache' / 'ccm' / 'meta').mkdir(parents=True, exist_ok=True)

    n = 0
    for f in HOOK_FILES:
        src = (HOOKS_SRC / f).resolve()
        dst = HOOKS_DST / f
        if not src.exists():
            print(f'  SKIP {f} (source missing)')
            continue
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        os.symlink(src, dst)
        n += 1
        print(f'  LINK {f} -> {src}')
    print(f'\n  {n} files symlinked into {HOOKS_DST}')
    return n


def install_bin_symlinks() -> int:
    """Symlink helper scripts into ~/.local/bin so they're invokable bare.

    Idempotent. If a name already exists in BIN_DST and is NOT a symlink
    pointing to our repo, leave it alone and warn — could be the user's
    own script with a colliding name.
    """
    BIN_DST.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in BIN_FILES:
        src = (HOOKS_SRC / f).resolve()
        dst = BIN_DST / f
        if not src.exists():
            print(f'  SKIP {f} (source missing)')
            continue
        if dst.is_symlink():
            try:
                if dst.resolve() == src:
                    print(f'  SKIP {f} (already linked)')
                    continue
            except OSError:
                pass
            dst.unlink()
        elif dst.exists():
            print(f'  SKIP {f} (exists in {BIN_DST} but not our symlink — left in place)')
            continue
        os.symlink(src, dst)
        n += 1
        print(f'  LINK {BIN_DST}/{f} -> {src}')
    print(f'\n  {n} helper(s) exposed on PATH via {BIN_DST}')
    return n


def remove_bin_symlinks() -> int:
    n = 0
    if not BIN_DST.is_dir():
        return 0
    for f in BIN_FILES:
        dst = BIN_DST / f
        if not dst.is_symlink():
            continue
        try:
            target = dst.resolve()
        except OSError:
            continue
        # Only remove symlinks that point into our repo. Never touch a
        # user-owned file with the same name.
        try:
            target.relative_to(REPO_ROOT)
        except ValueError:
            print(f'  SKIP {BIN_DST}/{f} (symlink target outside repo — left in place)')
            continue
        dst.unlink()
        n += 1
        print(f'  UNLINK {BIN_DST}/{f}')
    return n


def merge_settings() -> None:
    settings: dict = {}
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
        except json.JSONDecodeError:
            print(f'  WARNING: cannot parse {SETTINGS_FILE}, starting fresh')

    hooks = settings.setdefault('hooks', {})
    pretool = hooks.setdefault('PreToolUse', [])

    for event, matcher, command in HOOK_REGISTRATIONS:
        # Look for an existing entry with this matcher and our command.
        already = False
        for entry in pretool:
            if entry.get('matcher') == matcher:
                for h in entry.get('hooks', []):
                    if h.get('command') == command:
                        already = True
                        break
            if already:
                break
        if already:
            print(f'  SKIP {event}:{matcher} {command} (already registered)')
            continue
        # APPEND a new entry — preserves any existing matcher entries
        # (e.g. RTK's Bash hook). Order in settings.json determines
        # firing order, and we want to be last for Bash.
        pretool.append({
            'matcher': matcher,
            'hooks': [{'type': 'command', 'command': command}],
        })
        print(f'  ADD  {event}:{matcher} {command}')

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')
    print(f'\n  Settings written to {SETTINGS_FILE}')


def install_instructions() -> None:
    if not SNIPPET_FILE.exists():
        print(f'  SKIP CLAUDE.md (snippet missing at {SNIPPET_FILE})')
        return
    snippet_body = SNIPPET_FILE.read_text().rstrip('\n')
    block = f'\n{INSTRUCTIONS_BEGIN}\n{snippet_body}\n{INSTRUCTIONS_END}\n'

    CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
    existing = CLAUDE_MD.read_text() if CLAUDE_MD.exists() else ''

    if INSTRUCTIONS_BEGIN in existing and INSTRUCTIONS_END in existing:
        # Replace the managed block in place; leave surrounding text alone.
        before, _, rest = existing.partition(INSTRUCTIONS_BEGIN)
        _, _, after = rest.partition(INSTRUCTIONS_END)
        # Strip the leading newline we add so we don't accumulate blank
        # lines on repeated installs.
        new = before.rstrip() + '\n' + block.lstrip('\n') + after.lstrip('\n')
        CLAUDE_MD.write_text(new)
        print(f'  UPDATE CLAUDE.md routing-policy block ({CLAUDE_MD})')
    else:
        if existing and not existing.endswith('\n'):
            existing += '\n'
        CLAUDE_MD.write_text(existing + block)
        action = 'APPEND' if existing else 'CREATE'
        print(f'  {action} CLAUDE.md routing-policy block ({CLAUDE_MD})')


def remove_instructions() -> None:
    if not CLAUDE_MD.exists():
        return
    existing = CLAUDE_MD.read_text()
    if INSTRUCTIONS_BEGIN not in existing or INSTRUCTIONS_END not in existing:
        return
    before, _, rest = existing.partition(INSTRUCTIONS_BEGIN)
    _, _, after = rest.partition(INSTRUCTIONS_END)
    new = (before.rstrip() + '\n' + after.lstrip('\n')).strip() + '\n'
    if new.strip():
        CLAUDE_MD.write_text(new)
    else:
        CLAUDE_MD.unlink()
    print(f'  STRIP CLAUDE.md routing-policy block ({CLAUDE_MD})')


def remove() -> None:
    removed_files = 0
    for f in HOOK_FILES:
        dst = HOOKS_DST / f
        if dst.is_symlink():
            dst.unlink()
            removed_files += 1
            print(f'  UNLINK {f}')
        elif dst.exists():
            print(f'  SKIP {f} (not a symlink — left in place)')

    lib_dir = HOOKS_DST / 'lib'
    if lib_dir.is_dir() and not any(lib_dir.iterdir()):
        lib_dir.rmdir()
        print('  RMDIR lib/')

    removed_bin = remove_bin_symlinks()
    if removed_bin:
        print(f'  Removed {removed_bin} helper symlink(s) from {BIN_DST}')

    if not SETTINGS_FILE.exists():
        print('\n  No settings.json found')
        return

    settings = json.loads(SETTINGS_FILE.read_text())
    pretool = settings.get('hooks', {}).get('PreToolUse', [])
    our_commands = {cmd for _, _, cmd in HOOK_REGISTRATIONS}

    new_pretool = []
    removed_reg = 0
    for entry in pretool:
        kept_hooks = [h for h in entry.get('hooks', []) if h.get('command') not in our_commands]
        removed_reg += len(entry.get('hooks', [])) - len(kept_hooks)
        if kept_hooks:
            new_pretool.append({**entry, 'hooks': kept_hooks})

    if new_pretool:
        settings['hooks']['PreToolUse'] = new_pretool
    else:
        settings['hooks'].pop('PreToolUse', None)
        if not settings['hooks']:
            settings.pop('hooks')

    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')
    print(f'\n  Removed {removed_reg} hook registrations from {SETTINGS_FILE}')
    print(f'  Removed {removed_files} symlinks from {HOOKS_DST}')


def main() -> int:
    if '--check' in sys.argv:
        print_preflight(preflight())
        return 0
    if '--remove' in sys.argv:
        print('Removing claude-context-hooks...\n')
        remove()
        if '--no-instructions' not in sys.argv:
            remove_instructions()
        print('\nDone.')
        return 0

    print('Installing claude-context-hooks...\n')
    checks = preflight()
    if not print_preflight(checks):
        print('\nBlocking checks failed.')
        return 1
    print()
    install_files()
    print()
    install_bin_symlinks()
    print()
    merge_settings()
    if '--no-instructions' not in sys.argv:
        print()
        install_instructions()
    print('\nDone. Hooks activate on next Claude Code session.')
    print('Updates: git pull (symlinks update automatically).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
