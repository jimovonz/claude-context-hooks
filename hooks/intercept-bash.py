#!/usr/bin/env python3
"""
PreToolUse:Bash hook — wraps the command in cache-wrap.py via
hookSpecificOutput.updatedInput.command.

Layering: this hook is registered AFTER any RTK rewrite hook in
settings.json. Claude Code fires PreToolUse:Bash hooks in the order
they appear and propagates updatedInput between them, so by the time
this hook reads tool_input.command it is already the RTK-rewritten
form (e.g. `rtk git status` instead of `git status`).

We then rewrite once more to wrap the (possibly rtk-rewritten) command
in cache-wrap.py, which executes it and decides inline-vs-cache after
seeing real output size.

Exemptions (passed through unchanged): commands whose COMMAND WORD is one
of ours — ccm-get.py (already a retrieval), cache-wrap.py (already
wrapped), cch-batch.py (applies the same guards to each of its lines).
Merely mentioning one of those names no longer exempts a command.

All guard logic lives in lib/guards.py so cch-batch enforces exactly the
same rules; see that module for why.

The wrapper itself executes via `bash -c`, so all shell features work.
"""
import json
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from lib import guards
except Exception:                       # no guards -> pass through unwrapped
    guards = None
try:
    from lib.event_log import log_event
except Exception:                       # logging must never break a hook
    def log_event(*_args, **_kwargs):
        return None

WRAPPER_PATH = Path.home() / '.claude' / 'hooks' / 'cache-wrap.py'

# Re-exported for callers/tests that referenced these on this module.
PASSTHROUGH_MARKERS = (guards.PASSTHROUGH_MARKERS if guards is not None
                       else ('cache-wrap.py', 'ccm-get.py', 'cch-batch.py'))


def should_skip_wrap(cmd: str) -> bool:
    if guards is None:
        return True
    return guards.is_passthrough(cmd)


def _deny(reason: str) -> int:
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        }
    }
    json.dump(response, sys.stdout)
    sys.stdout.write('\n')
    return 0


def main() -> int:
    if os.environ.get('CCH_DISABLE') == '1':
        sys.stdout.write('{}\n')
        return 0

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.stdout.write('{}\n')
        return 0

    tool_input = data.get('tool_input') or {}
    cmd = tool_input.get('command', '')
    sid = str(data.get('session_id') or '')[:36]
    cwd = data.get('cwd') or os.getcwd()

    if should_skip_wrap(cmd):
        sys.stdout.write('{}\n')
        return 0

    # Blocking guards (bulk code-file read, symbol-grep answerable from the
    # graph). A repeat of the identical command overrides once.
    reason = guards.block(cmd, cwd)
    if reason:
        log_event('deny_bash_guard', sid=sid, cmd_head=cmd[:120],
                  kind='graph' if 'graph:' in reason else 'bulk_read')
        return _deny(reason)

    # Non-blocking warnings, prepended safely (never via an unquoted echo).
    cmd, applied = guards.apply_warnings(cmd)
    for w in applied:
        event = ('warn_rg_replace' if 'rg -r' in w else 'warn_bulk_sed')
        log_event(event, sid=sid, cmd_head=cmd[:120])

    env_prefix = f'CCH_SESSION_ID={shlex.quote(sid)} ' if sid else ''
    wrapped = f'{env_prefix}{WRAPPER_PATH} -- {shlex.quote(cmd)}'
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'updatedInput': {
                **tool_input,
                'command': wrapped,
            },
        }
    }
    json.dump(response, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    # This hook gates EVERY Bash call. Any unhandled failure must allow the
    # command through unwrapped, never break the session.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.stdout.write('{}\n')
        sys.exit(0)
