"""Command guards shared by the PreToolUse:Bash hook and cch-batch.

These lived in intercept-bash.py alone, which cch-batch bypasses by
construction: `cch-batch.py` is in PASSTHROUGH_MARKERS, so the hook skips the
whole batch and cch-batch shells each line straight into cache-wrap. Batched
commands therefore escaped the bulk-read block, the graph answer and the
rg -r warning entirely — the documented power-tool was also the documented
bypass. Both entry points now call the same functions here.

Two kinds of guard:
  block(cmd, cwd)  -> reason string, or None to allow
  warnings(cmd)    -> list of non-blocking warning lines to prepend

Blocks are one-shot overridable: re-running the identical command clears the
marker and lets it through, which is what the block message has always
claimed ("rerun only if you need every text occurrence") and never did.
"""
# Annotations are never evaluated, so `Optional` costs nothing at runtime and
# typing (~1.6ms) need not be imported at all. The three heavy imports that
# remain are deferred to the functions that need them: this module is imported
# by the PreToolUse:Bash hook on *every* Bash call, and the common path — a
# command that trips no guard — touches none of them.
from __future__ import annotations

import os
import re
import shlex
import time
from pathlib import Path

# Commands that must never be re-wrapped or re-guarded: retrieval and the
# wrapper itself (recursive stubbing), plus the batch runner (which applies
# these guards to each of its own lines).
PASSTHROUGH_MARKERS = ('cache-wrap.py', 'ccm-get.py', 'cch-batch.py')

_CODE_EXTS = frozenset({
    '.py', '.js', '.ts', '.tsx', '.jsx', '.rs', '.go',
    '.java', '.rb', '.c', '.cpp', '.h', '.hpp', '.cs',
    '.ex', '.exs',
})

_BULK_THRESHOLD = 50
_BULK_WARN_THRESHOLD = 100
_BULK_WARN_PREFIX = (
    "[cch: reading {lines} lines from {path} — consider "
    "cairn-graph --location SYMBOL for targeted reads]"
)
_BULK_READ_REDIRECT = (
    "BLOCKED: Run cairn-graph --location SYMBOL first, then sed -n 'A,Bp;Bq' on "
    "the result."
)
_OVERRIDE_HINT = " (rerun the identical command to override once)"

_RG_REPLACE_WARN = (
    "[cch: rg -r / --replace REWRITES every match in the output "
    "(ripgrep -r is --replace, NOT recursive; rg recurses by default). "
    "If you meant recursive, drop -r.]"
)

_DEF_KEYWORDS = (
    r"def|class|function|fn|func|type|interface|struct|enum|trait|impl|module"
)

_PATTERNS = [
    (re.compile(
        rf"""(?:grep|rg)\b.*(?:'|")(?:{_DEF_KEYWORDS})\s+(\w+)(?:'|")"""
    ), "location"),
    # `grep "foo("` means "who calls foo". The quote after the paren must be
    # the SAME quote that opened the pattern — otherwise a search for a call
    # WITH a string argument (grep "add_argument('--dist'") reads as a symbol
    # query, and blocks a literal-text search that the graph cannot serve.
    (re.compile(
        r"""(?:grep|rg)\b.*(['"])\\?\.?(\w{2,})\(\1"""
    ), "callers"),
    (re.compile(
        r"""(?:grep|rg)\b.*?(?:['"]|\s)(?:def\s+)?(test_\w+)(?:['"]|\s|$)"""
    ), "tests"),
    (re.compile(
        r"""(?:grep|rg)\b.*(?:'|")(?:from\s+(\w+)\s+import|import\s+(\w+))(?:'|")"""
    ), "callees"),
]

# One-shot override markers. Keyed by command hash so only the identical
# command passes; pruned on the same weekly cadence as the rules markers.
_OVERRIDE_DIR = Path.home() / '.claude' / 'cache' / 'cch' / 'blocked-seen'
_OVERRIDE_TTL_S = 7 * 86400


# ---------------------------------------------------------------- shell shape

_SEGMENT_RE = re.compile(r'\|\||&&|[|;&]')


def segments(cmd: str) -> list[str]:
    """Shell segments of a command line (split on | || && ; &).

    Quote-aware: a separator *inside* quotes is data, not a separator. The
    naive split fragmented `sed -n '1,200p;200q' f.py` into two segments and
    so silently lost warn_bulk_sed — and the early-quit form is exactly what
    we now tell the model to use, since plain `sed -n 'A,Bp'` reads to EOF.
    """
    out: list[str] = []
    buf: list[str] = []
    quote = None
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if quote is not None:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in '\'"':
            quote = c
            buf.append(c)
            i += 1
            continue
        m = _SEGMENT_RE.match(cmd, i)
        if m:
            out.append(''.join(buf))
            buf = []
            i = m.end()
            continue
        buf.append(c)
        i += 1
    out.append(''.join(buf))
    return [s.strip() for s in out if s.strip()]


def _argv0(segment: str) -> str:
    """Basename of a segment's command word, ignoring VAR=val prefixes."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        toks = segment.split()
    for tok in toks:
        if '=' in tok and not tok.startswith('/') and re.match(r'^\w+=', tok):
            continue  # env assignment prefix
        return os.path.basename(tok)
    return ''


def is_passthrough(cmd: str) -> bool:
    """True when this command is one of ours and must not be wrapped.

    Matches the COMMAND WORD of any segment, not a substring of the whole
    line: `rg -n cache-wrap.py hooks/` merely mentions a marker and must
    still be wrapped and guarded.
    """
    if not cmd or not cmd.strip():
        return True
    return any(_argv0(seg) in PASSTHROUGH_MARKERS for seg in segments(cmd))


def _strip_rtk(segment: str) -> str:
    return re.sub(r'^rtk\s+', '', segment.strip())


# ------------------------------------------------------------------- bulk read

def _extract_code_file(cmd_tail: str) -> Optional[str]:
    """Return the last non-flag token if it has a code extension."""
    for tok in reversed(cmd_tail.split()):
        if not tok.startswith('-'):
            # Strip quotes before testing the suffix. `cat 'foo.py'` tokenises
            # to `'foo.py'`, whose suffix is `.py'` — matching no code
            # extension — so a single quote character silently bypassed the
            # bulk-read block entirely. Paths with spaces need quoting, so
            # this was reachable by accident, not just deliberately.
            tok = tok.strip('\'"')
            if Path(tok).suffix.lower() in _CODE_EXTS:
                return tok
            return None
    return None


def _bulk_read_segment(segment: str) -> Optional[str]:
    effective = _strip_rtk(segment)

    if re.match(r'^cat\b', effective):
        if _extract_code_file(effective[3:]):
            return _BULK_READ_REDIRECT
        return None

    m = re.match(r'^(head|tail)\b(.*)', effective)
    if m:
        rest = m.group(2)
        n_match = re.search(r'-n\s*(\d+)|-(\d+)', rest)
        if not n_match:
            return None  # no -n = default 10, fine
        n = int(n_match.group(1) or n_match.group(2))
        if n < _BULK_THRESHOLD:
            return None
        if _extract_code_file(rest):
            return _BULK_READ_REDIRECT
        return None
    return None


def check_bulk_read(cmd: str) -> Optional[str]:
    """Detect bulk reads of code files in ANY segment of the command.

    Previously only the first pipe segment was inspected, so `true && cat
    foo.py` walked straight through.
    """
    for seg in segments(cmd):
        reason = _bulk_read_segment(seg)
        if reason:
            return reason
    return None


# -------------------------------------------------------------------- warnings

def warn_bulk_sed(cmd: str) -> Optional[str]:
    """Warning for large `sed -n A,Bp` reads of code files, or None."""
    for seg in segments(cmd):
        effective = _strip_rtk(seg)
        m = re.match(r"""^sed\s+-n\s+['"]?(\d+),(\d+)p(?:\s*;\s*\d*q)?['"]?(.*)""", effective)
        if not m:
            continue
        lines = int(m.group(2)) - int(m.group(1))
        if lines < _BULK_WARN_THRESHOLD:
            continue
        path = _extract_code_file(m.group(3))
        if path:
            return _BULK_WARN_PREFIX.format(lines=lines, path=path)
    return None


def warn_rg_replace(cmd: str) -> Optional[str]:
    """Warn when rg is invoked with -r/--replace (grep's -r habit), else None."""
    for seg in segments(cmd):
        effective = _strip_rtk(seg)
        try:
            toks = shlex.split(effective)
        except ValueError:
            continue
        if not toks or os.path.basename(toks[0]) != 'rg':
            continue
        for tok in toks[1:]:
            if tok == '--':
                break
            if tok == '--replace' or tok.startswith('--replace='):
                return _RG_REPLACE_WARN
            if tok.startswith('-') and not tok.startswith('--') and 'r' in tok[1:]:
                return _RG_REPLACE_WARN
    return None


def warnings(cmd: str) -> list[str]:
    """All non-blocking warning lines that apply to this command."""
    return [w for w in (warn_bulk_sed(cmd), warn_rg_replace(cmd)) if w]


def prefix_warning(cmd: str, message: str) -> str:
    """Prepend a warning line to a command without shell-injecting it.

    The message interpolates a path token lifted from the user command, so
    embedding it in a double-quoted echo executes $(...) and backticks
    (verified with `sed -n 1,200p $(id).py`).
    """
    return "printf '%s\\n' " + shlex.quote(message) + f"; {cmd}"


def apply_warnings(cmd: str) -> tuple[str, list[str]]:
    """Return (possibly prefixed command, warnings applied)."""
    applied = warnings(cmd)
    for w in applied:
        cmd = prefix_warning(cmd, w)
    return cmd, applied


# --------------------------------------------------------------- graph answers

def graph_db_for(cwd: str) -> Optional[Path]:
    """Walk up from cwd to find .code-review-graph/graph.db."""
    d = Path(cwd or '.').resolve()
    while True:
        candidate = d / '.code-review-graph' / 'graph.db'
        if candidate.is_file():
            return candidate
        if d.parent == d:
            return None
        d = d.parent


def graph_answer(graph_db: Path, redirect_type: str, symbol: str) -> Optional[str]:
    """Answer a symbol query straight from graph.db, or None if unresolvable.

    Only returns a string when the graph genuinely has the answer — a miss
    must NOT block the grep (the graph may be stale or the language's edge
    extraction thin).
    """
    # Deferred: sqlite3 drags in datetime behind it, ~2.8ms that every Bash
    # call was paying so that a symbol-shaped grep could be answered.
    import sqlite3

    conn = None
    try:
        conn = sqlite3.connect(str(graph_db))
        conn.execute("PRAGMA busy_timeout=500")
        if redirect_type == "location":
            # Fetch one more than we will show: a symbol defined in many places
            # (per-module fallback stubs, an overridden method) has no single
            # answer, and a truncated list reads as if it did. Ambiguity is a
            # reason to let the grep run, not to block it.
            rows = conn.execute(
                "SELECT file_path, line_start, line_end FROM nodes "
                "WHERE name = ? AND kind IN ('Function', 'Class', 'Type') "
                "ORDER BY line_start LIMIT 4",
                (symbol,),
            ).fetchall()
            if len(rows) > 3:
                return None
            if rows:
                locs = " · ".join(f"{f}:{a}-{b}" for f, a, b in rows)
                return (
                    f"graph: {symbol} → {locs}. "
                    f"Body: sed -n 'A,Bp;Bq' on that span."
                )
        elif redirect_type in ("callers", "tests", "callees"):
            # Only answer CALLERS for symbols this repo actually DEFINES. Library calls
            # (add_argument, get, append, print) resolve as edge targets with
            # huge fan-in, and their caller list — "main · main · main" — is
            # noise that blocks a legitimate search for nothing. A local
            # definition node is what makes a symbol the graph's business. Scoped
            # to callers: the tests pattern is test_-prefixed and the callees
            # pattern is import-shaped, so neither can resolve to a library hub,
            # and requiring a node there breaks thin-extraction repos where the
            # edge exists without a definition row.
            if redirect_type == 'callers':
                defined = conn.execute(
                    "SELECT 1 FROM nodes WHERE name = ? "
                    "AND kind IN ('Function', 'Class', 'Type') LIMIT 1",
                    (symbol,),
                ).fetchone()
                if not defined:
                    return None
            edge_kind = {"callers": "CALLS", "tests": "TESTED_BY",
                         "callees": "CALLS"}[redirect_type]
            # TESTED_BY is stored as (source=tested symbol, target=test), the
            # same direction as CALLS for callees — so the symbol is looked
            # up in source_qualified, not target. Searching target here
            # returned nothing for every symbol in the repo.
            col, other = (("source_qualified", "target_qualified")
                          if redirect_type in ("callees", "tests")
                          else ("target_qualified", "source_qualified"))
            rows = conn.execute(
                f"SELECT DISTINCT {other} FROM edges "
                f"WHERE kind = ? AND ({col} LIKE ? OR {col} = ? "
                f"OR {col} LIKE ?) LIMIT 6",
                (edge_kind, f"%::{symbol}", symbol, f"%.{symbol}"),
            ).fetchall()
            if rows:
                names = " · ".join(r[0].rsplit("::", 1)[-1] for r in rows)
                return (
                    f"graph: {symbol} {redirect_type}: {names} "
                    f"(cairn-graph --{redirect_type} {symbol} for locations)"
                )
        return None
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def check_symbol_grep(cmd: str, cwd: str = '') -> Optional[str]:
    """Answer a grep-for-symbol from the graph; None to let the grep run."""
    for pattern, redirect_type in _PATTERNS:
        m = pattern.search(cmd)
        if not m:
            continue
        # Skip a captured quote delimiter (the callers pattern backreferences
        # it); the symbol is the first group that is not a lone quote char.
        symbol = next((g for g in m.groups()
                       if g is not None and g not in ('"', "'")), None)
        if symbol is None:
            return None
        graph_db = graph_db_for(cwd)
        if graph_db is None:
            return None
        answer = graph_answer(graph_db, redirect_type, symbol)
        if answer:
            return f"BLOCKED: {answer}"
        return None
    return None


# ---------------------------------------------------------------- one-shot pass

def _override_marker(cmd: str) -> Path:
    # Deferred: only reached after a guard has already fired, since block()
    # calls consume_override solely on a non-None reason.
    import hashlib

    digest = hashlib.blake2s(cmd.encode('utf-8', 'replace'),
                             digest_size=8).hexdigest()
    return _OVERRIDE_DIR / digest


def _prune_overrides() -> None:
    try:
        cutoff = time.time() - _OVERRIDE_TTL_S
        for m in _OVERRIDE_DIR.iterdir():
            if m.stat().st_mtime < cutoff:
                m.unlink(missing_ok=True)
    except OSError:
        pass


def consume_override(cmd: str) -> bool:
    """True when this exact command was blocked before — and clears the mark.

    Makes the block message's "rerun to override" true, exactly once. The
    old _SESSION_MARKER constant promised this and was never read.
    """
    marker = _override_marker(cmd)
    try:
        _OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            marker.unlink(missing_ok=True)
            return True
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        _prune_overrides()
    except FileExistsError:
        return True
    except OSError:
        return False
    return False


_PYTEST_EXIT_MASKED = (
    "pytest piped into head/tail discards its exit status — a pipeline reports "
    "the LAST command's code, so a failing suite reads as success. This is not "
    "hypothetical: it produced a false green that was committed on. Either drop "
    "the pipe and read the summary line, or make the status survive: "
    "`set -o pipefail; pytest ... | tail -3`."
)

_PYTEST_PIPE_RE = re.compile(r'\bpytest\b[^|]*\|\s*(?:tail|head)\b')


def check_pytest_exit_masked(cmd: str) -> Optional[str]:
    """Block a pytest pipeline whose exit status is thrown away.

    `pytest ... | tail -2` is the natural way to keep output short, and it is
    exactly why the failure is easy to miss: the summary line scrolls past while
    the shell reports 0. `set -o pipefail` restores the real status, so the
    guard asks for that rather than banning the pipe.
    """
    if 'pipefail' in cmd:
        return None
    return _PYTEST_EXIT_MASKED if _PYTEST_PIPE_RE.search(cmd) else None


_SELF_REPLACE = (
    "A global replace whose REPLACEMENT contains the SEARCH string rewrites its "
    "own output: every later match is one the substitution itself produced. Drop "
    "--all and edit the occurrences you mean, or make the replacement not "
    "contain the original text."
)


def check_self_referential_replace(cmd: str) -> Optional[str]:
    """Block `cch-edit --all OLD NEW` when NEW contains OLD."""
    for seg in segments(cmd):
        try:
            toks = shlex.split(_strip_rtk(seg))
        except ValueError:
            continue
        if not toks or not os.path.basename(toks[0]).startswith('cch-edit'):
            continue
        if '--all' not in toks:
            continue
        positionals = [t for t in toks[1:] if not t.startswith('-')]
        # cch-edit.py PATH OLD NEW
        if len(positionals) >= 3 and positionals[1] and positionals[1] in positionals[2]:
            return _SELF_REPLACE
    return None


def block(cmd: str, cwd: str = '') -> Optional[str]:
    """The blocking guard. Returns a reason, or None to allow the command.

    A repeat of the identical command passes through (one-shot override), so
    the model always has a way forward without a second tool round-trip
    guessing at what would satisfy the guard.
    """
    reason = (check_bulk_read(cmd) or check_symbol_grep(cmd, cwd)
              or check_pytest_exit_masked(cmd) or check_self_referential_replace(cmd))
    if not reason:
        return None
    if consume_override(cmd):
        return None
    return reason + _OVERRIDE_HINT
