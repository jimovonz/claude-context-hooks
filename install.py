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
    python install.py                       # install hooks + RTK + instructions
    python install.py --skip-rtk            # install hooks only, no RTK download
    python install.py --skip-search-tools   # don't auto-install ripgrep + fd
    python install.py --no-instructions     # install hooks only
    python install.py --remove              # uninstall both
    python install.py --remove --no-instructions  # leave CLAUDE.md alone
    python install.py --check               # pre-flight checks only
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RTK_RELEASES_API = "https://api.github.com/repos/rtk-ai/rtk/releases/latest"

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
    'intercept-agent.py',
    'cache-wrap.py',
    'cch-batch.py',
    'cch-edit.py',
    'cch-write.py',
    'ccm-get.py',
    'lib/__init__.py',
    'lib/ccm_cache.py',
    'lib/event_log.py',
    'lib/cairn_graph_footer.py',
    'cch-gain.py',
]

# Helpers also symlinked into ~/.local/bin/ so the model can invoke
# them bare (`cch-edit.py PATH old new`) without absolute paths.
# These are installed only into BIN_DST; the canonical copies live in
# HOOKS_DST. We never overwrite an existing entry that points elsewhere
# (could be the user's own script with a colliding name).
BIN_FILES = [
    'cch-batch.py',
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
    ('PreToolUse', 'Agent',        '~/.claude/hooks/intercept-agent.py'),
]


def preflight() -> dict:
    """Return a dict of check_name -> (ok, message)."""
    checks = {}
    rtk = shutil.which('rtk')
    checks['rtk_on_path'] = (
        rtk is not None,
        f"rtk found at {rtk}" if rtk else
        "rtk not found — will auto-install (pass --skip-rtk to skip)"
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
    rg = shutil.which('rg')
    fd = shutil.which('fd') or shutil.which('fdfind')
    missing = [n for n, p in (('ripgrep', rg), ('fd', fd)) if p is None]
    checks['search_tools'] = (
        not missing,
        'ripgrep + fd on PATH — Grep/Glob redirects resolve'
        if not missing else
        f"missing {', '.join(missing)} — will auto-install (pass --skip-search-tools "
        "to skip); Grep/Glob hooks fall back to grep/find meanwhile"
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


def _rtk_asset_name() -> str:
    """Return the RTK release asset name for the current platform, or empty string."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "rtk-x86_64-unknown-linux-musl.tar.gz"
        if machine in ("aarch64", "arm64"):
            return "rtk-aarch64-unknown-linux-gnu.tar.gz"
    elif system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "rtk-aarch64-apple-darwin.tar.gz"
        return "rtk-x86_64-apple-darwin.tar.gz"
    return ""


def _rtk_init() -> bool:
    """Run rtk init -g --auto-patch to register the Claude Code hook."""
    rtk = shutil.which("rtk") or str(BIN_DST / "rtk")
    try:
        result = subprocess.run(
            [rtk, "init", "-g", "--auto-patch"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("  INIT  rtk init -g --auto-patch")
            return True
        print(f"  !! rtk init failed: {result.stderr.strip()}")
        return False
    except Exception as exc:
        print(f"  !! rtk init error: {exc}")
        return False


def install_rtk() -> bool:
    """Download RTK binary to ~/.local/bin and register its Claude Code hook.

    Returns True when RTK is available (already installed or just installed).
    Pass --skip-rtk on the command line to skip entirely.
    """
    if shutil.which("rtk"):
        print("  OK rtk already on PATH")
        _rtk_init()
        return True

    asset = _rtk_asset_name()
    if not asset:
        print("  !! rtk: unsupported platform — install manually from https://github.com/rtk-ai/rtk")
        return False

    try:
        print("  Fetching RTK release info...")
        with urllib.request.urlopen(RTK_RELEASES_API, timeout=15) as resp:
            release = json.loads(resp.read())
    except Exception as exc:
        print(f"  !! rtk: cannot fetch release info: {exc}")
        return False

    version = release.get("tag_name", "unknown")
    url = next(
        (a["browser_download_url"] for a in release.get("assets", []) if a["name"] == asset),
        None,
    )
    if not url:
        print(f"  !! rtk: asset {asset!r} not in {version} release — install manually")
        return False

    try:
        print(f"  Downloading RTK {version} ({asset})...")
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, asset)
            urllib.request.urlretrieve(url, archive)
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if member.name in ("rtk", "./rtk") or member.name.endswith("/rtk"):
                        member.name = "rtk"
                        tf.extract(member, path=tmp, filter="data")
                        break
            src = os.path.join(tmp, "rtk")
            if not os.path.exists(src):
                print("  !! rtk: binary not found in archive")
                return False
            BIN_DST.mkdir(parents=True, exist_ok=True)
            dst = BIN_DST / "rtk"
            shutil.copy2(src, dst)
            dst.chmod(0o755)
            print(f"  INSTALL rtk {version} -> {dst}")
    except Exception as exc:
        print(f"  !! rtk: download/install failed: {exc}")
        return False

    return _rtk_init()


# Package names per manager for the two search binaries the Grep/Glob
# redirects target. Debian/Ubuntu ship fd's binary as `fdfind`.
SEARCH_TOOL_PKGS = {
    'apt-get': (['apt-get', 'install', '-y'], True, {'rg': 'ripgrep', 'fd': 'fd-find'}),
    'dnf':     (['dnf', 'install', '-y'], True, {'rg': 'ripgrep', 'fd': 'fd-find'}),
    'zypper':  (['zypper', 'install', '-y'], True, {'rg': 'ripgrep', 'fd': 'fd'}),
    'pacman':  (['pacman', '-S', '--noconfirm'], True, {'rg': 'ripgrep', 'fd': 'fd'}),
    'brew':    (['brew', 'install'], False, {'rg': 'ripgrep', 'fd': 'fd'}),
}


def _ensure_fd_symlink() -> None:
    """Debian/Ubuntu install fd's binary as `fdfind`, but the routing
    config calls it `fd`. Symlink into ~/.local/bin (user-space, no sudo)."""
    if shutil.which('fd'):
        return
    fdfind = shutil.which('fdfind')
    if not fdfind:
        return
    BIN_DST.mkdir(parents=True, exist_ok=True)
    link = BIN_DST / 'fd'
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(fdfind)
        print(f'  LINK fd -> {fdfind}')
    except OSError as exc:
        print(f'  !! could not symlink fd: {exc}')


def install_search_tools() -> bool:
    """Best-effort install of ripgrep + fd — the binaries the Grep/Glob
    redirects point at. Non-fatal: the hooks fall back to grep/find when
    these are absent, so a failed install only costs RTK compression on
    those paths. Pass --skip-search-tools to skip."""
    need = []
    if not shutil.which('rg'):
        need.append('rg')
    if not (shutil.which('fd') or shutil.which('fdfind')):
        need.append('fd')
    if not need:
        print('  OK ripgrep + fd already on PATH')
        _ensure_fd_symlink()
        return True

    mgr = next((m for m in SEARCH_TOOL_PKGS if shutil.which(m)), None)
    if mgr is None:
        print('  !! no supported package manager found — install ripgrep + fd '
              'manually (Grep/Glob hooks fall back to grep/find meanwhile)')
        return False

    prefix, needs_sudo, pkgmap = SEARCH_TOOL_PKGS[mgr]
    pkgs = [pkgmap[t] for t in need]
    use_sudo = needs_sudo and getattr(os, 'geteuid', lambda: 0)() != 0
    cmd = (['sudo'] + prefix if use_sudo else prefix) + pkgs
    print(f"  Installing {' + '.join(pkgs)} via {mgr}"
          f"{' (sudo)' if use_sudo else ''}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f'  !! {mgr} install failed: {result.stderr.strip()[:200]}')
            return False
        print(f'  INSTALL {" + ".join(pkgs)}')
    except Exception as exc:
        print(f'  !! search-tool install error: {exc}')
        return False
    _ensure_fd_symlink()
    return True


def _fix_bash_hook_order() -> None:
    """Ensure RTK fires before CCH in PreToolUse:Bash — reorders in-place if needed."""
    if not SETTINGS_FILE.exists():
        return
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
    except json.JSONDecodeError:
        return
    pretool = settings.get("hooks", {}).get("PreToolUse", [])

    bash_rtk, bash_cch, others = None, None, []
    for entry in pretool:
        if entry.get("matcher") == "Bash":
            cmd = entry["hooks"][0].get("command", "") if entry.get("hooks") else ""
            if "rtk" in cmd and bash_rtk is None:
                bash_rtk = entry
            elif "intercept-bash" in cmd and bash_cch is None:
                bash_cch = entry
            else:
                others.append(entry)
        else:
            others.append(entry)

    if bash_rtk is None or bash_cch is None:
        return  # nothing to reorder

    # Find current positions to detect if already correct
    flat = [e for e in pretool if e.get("matcher") == "Bash"]
    if flat and flat[0] is bash_rtk:
        return  # already correct

    settings["hooks"]["PreToolUse"] = others + [bash_rtk, bash_cch]
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")
    print("  ORDER PreToolUse:Bash — RTK first, CCH second")


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
        new = existing.rstrip("\n") + "\n" + block.lstrip("\n") if existing else block.lstrip("\n")
        CLAUDE_MD.write_text(new)
        action = "APPEND" if existing else "CREATE"
        print(f"  {action} CLAUDE.md routing-policy block ({CLAUDE_MD})")


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
    if "--skip-rtk" not in sys.argv:
        print()
        install_rtk()
    if "--skip-search-tools" not in sys.argv:
        print()
        install_search_tools()
    print()
    merge_settings()
    print()
    _fix_bash_hook_order()
    if "--no-instructions" not in sys.argv:
        print()
        install_instructions()
    print('\nDone. Hooks activate on next Claude Code session.')
    print('Updates: git pull (symlinks update automatically).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
