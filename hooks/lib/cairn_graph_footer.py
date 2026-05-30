"""Generate cairn-graph footer for code-file read commands.

Function-level: parses sed -n line ranges, resolves which functions
overlap the viewed lines, and queries callers/tests per function.
Per-function-per-session dedup: footer shown once per qualified_name.
"""

import json
import os
import re
import shlex
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SOURCE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.c', '.cpp', '.cc',
    '.cxx', '.h', '.hpp', '.java', '.rb', '.sh', '.sql', '.kt', '.swift',
    '.cs', '.scala', '.lua', '.zig', '.hs', '.ex', '.exs', '.erl', '.ml',
    '.mli', '.r', '.R', '.jl', '.pl', '.pm',
}

READ_COMMANDS = {'cat', 'head', 'tail', 'sed'}

_SESSION_SEEN_FILE = Path(tempfile.gettempdir()) / f'cch-footer-seen-{os.getppid()}'


def _load_seen() -> set:
    try:
        return set(json.loads(_SESSION_SEEN_FILE.read_text()))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()


def _save_seen(seen: set) -> None:
    try:
        _SESSION_SEEN_FILE.write_text(json.dumps(sorted(seen)))
    except OSError:
        pass


def _extract_line_range(command: str) -> tuple[Optional[int], Optional[int]]:
    """Extract (start, end) line range from sed -n or head -n commands."""
    stripped = re.sub(r'^rtk\s+', '', command.strip())
    m = re.match(r"""^sed\s+-n\s+['"](\d+),(\d+)p['"]""", stripped)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r'^head\b.*?(?:-n\s*|-(?=\d))(\d+)', stripped)
    if m:
        return 1, int(m.group(1))
    return None, None


def _extract_source_file(command: str, cwd: str) -> Optional[str]:
    """Extract source file path from a code-file read command."""
    if '|' in command:
        return None
    # Handle cd prefix: "cd /path && cmd" — extract effective cwd
    if command.strip().startswith("cd ") and "&&" in command:
        parts = command.split("&&", 1)
        cd_part = parts[0].strip()
        command = parts[1].strip()
        cd_target = cd_part[3:].strip().rstrip(';')
        if cd_target:
            cwd = cd_target
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None

    # Skip RTK prefix if present
    start = 0
    if os.path.basename(tokens[0]) == "rtk" and len(tokens) > 1:
        start = 1
    base = os.path.basename(tokens[start])
    if base not in READ_COMMANDS:
        return None

    # Find file argument: skip flags (tokens starting with -)
    # For sed, also skip the script argument (quoted or unquoted)
    candidates = []
    i = start + 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith('-'):
            # Skip flag and its value if it looks like -n NUM
            if len(tok) == 2 and tok[1].isalpha() and i + 1 < len(tokens):
                next_tok = tokens[i + 1]
                if not next_tok.startswith('-') and not os.path.sep in next_tok and not '.' in next_tok:
                    i += 2
                    continue
            i += 1
            continue
        candidates.append(tok)
        i += 1

    # For sed, the last candidate is the file, earlier ones are scripts
    if base == 'sed' and len(candidates) >= 2:
        file_arg = candidates[-1]
    elif candidates:
        file_arg = candidates[-1]
    else:
        return None

    # Resolve path
    p = Path(file_arg)
    if not p.is_absolute():
        p = Path(cwd) / p
    p = p.resolve()

    if p.suffix not in SOURCE_EXTENSIONS:
        return None

    return str(p)


def _find_graph_db(start_dir: str) -> Optional[Path]:
    """Walk up from start_dir to find .code-review-graph/graph.db."""
    d = Path(start_dir).resolve()
    while True:
        candidate = d / '.code-review-graph' / 'graph.db'
        if candidate.is_file():
            return candidate
        parent = d.parent
        if parent == d:
            return None
        d = parent


def _find_cairn_db() -> Optional[Path]:
    """Locate cairn.db."""
    cairn_home = os.environ.get('CAIRN_HOME', os.path.expanduser('~/Projects/cairn'))
    p = Path(cairn_home) / 'cairn' / 'cairn.db'
    return p if p.is_file() else None


def _resolve_functions(graph_db: Path, file_path: str,
                       line_start: Optional[int],
                       line_end: Optional[int]) -> list[dict]:
    """Resolve functions overlapping the viewed line range."""
    conn = sqlite3.connect(str(graph_db))
    conn.execute("PRAGMA busy_timeout=1000")

    if line_start is not None and line_end is not None:
        nodes = conn.execute(
            "SELECT qualified_name, name FROM nodes "
            "WHERE file_path = ? AND kind IN ('Function', 'Class') "
            "AND line_start <= ? AND line_end >= ? "
            "ORDER BY line_start",
            (file_path, line_end, line_start),
        ).fetchall()
    else:
        nodes = conn.execute(
            "SELECT qualified_name, name FROM nodes "
            "WHERE file_path = ? AND kind IN ('Function', 'Class')",
            (file_path,),
        ).fetchall()

    results = []
    for qn, name in nodes:
        callers = conn.execute(
            "SELECT COUNT(*) FROM edges "
            "WHERE kind = 'CALLS' AND target_qualified = ?",
            (qn,),
        ).fetchone()[0]
        tests = conn.execute(
            "SELECT COUNT(*) FROM edges "
            "WHERE kind = 'TESTED_BY' AND source_qualified = ?",
            (qn,),
        ).fetchone()[0]
        results.append({
            'qualified_name': qn,
            'name': name,
            'callers': callers,
            'tests': tests,
        })

    conn.close()
    return results


def _query_cairn(cairn_db: Path, file_path: str,
                 qualified_names: list[str]) -> list:
    """Find high-confidence correction/decision memories tagged to file or functions."""
    conn = sqlite3.connect(str(cairn_db))
    conn.execute("PRAGMA busy_timeout=1000")

    rows = conn.execute(
        "SELECT type, content, updated_at FROM memories "
        "WHERE type IN ('correction', 'decision') "
        "AND deleted_at IS NULL "
        "AND confidence >= 0.7 "
        "AND associated_files LIKE ? "
        "ORDER BY updated_at DESC LIMIT 2",
        (f'%{file_path}%',),
    ).fetchall()

    conn.close()
    return [{'type': r[0], 'content': r[1], 'updated_at': r[2]} for r in rows]


def _format_memory(m: dict) -> str:
    """Format a single cairn memory for the footer."""
    try:
        dt = datetime.fromisoformat(m['updated_at'].replace('Z', '+00:00'))
        days = (datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)).days
    except (ValueError, AttributeError):
        days = 0
    snippet = m['content'][:60]
    return f'{m["type"]} {days}d ago: "{snippet}"'


def _format_footer(functions: list[dict], memories: list) -> Optional[str]:
    """Build footer string from per-function data, max 200 chars."""
    if not functions and not memories:
        return None

    parts = []
    for fn in functions:
        parts.append(f'{fn["name"]}: {fn["callers"]} callers · {fn["tests"]} tests')

    for m in memories[:2]:
        parts.append(_format_memory(m))

    footer = '[cairn-graph: ' + ' · '.join(parts) + ']'

    if len(footer) > 200:
        footer = footer[:197] + '...]'

    return footer


def generate_footer(command: str, cwd: str) -> Optional[str]:
    """Generate cairn-graph footer for a code-file read command.
    Best-effort: returns None on any error. Never raises.
    Function-scoped: resolves which functions overlap the viewed lines.
    Per-function dedup: footer shown once per qualified_name per session.
    """
    try:
        file_path = _extract_source_file(command, cwd)
        if file_path is None:
            return None

        graph_db = _find_graph_db(str(Path(file_path).parent))
        if graph_db is None:
            return None

        line_start, line_end = _extract_line_range(command)
        functions = _resolve_functions(graph_db, file_path, line_start, line_end)

        # Dedup: filter out functions already shown this session
        seen = _load_seen()
        new_functions = [f for f in functions if f['qualified_name'] not in seen]
        if not new_functions:
            # All functions already footered — check if there are memories to show
            cairn_db = _find_cairn_db()
            if cairn_db:
                qns = [f['qualified_name'] for f in functions]
                memories = _query_cairn(cairn_db, file_path, qns)
                if memories:
                    return _format_footer([], memories)
            return None

        # Mark new functions as seen
        seen.update(f['qualified_name'] for f in new_functions)
        _save_seen(seen)

        memories = []
        cairn_db = _find_cairn_db()
        if cairn_db:
            qns = [f['qualified_name'] for f in new_functions]
            memories = _query_cairn(cairn_db, file_path, qns)

        return _format_footer(new_functions, memories)
    except Exception:
        return None
