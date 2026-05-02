"""Generate cairn-graph footer for code-file read commands."""

import os
import shlex
import sqlite3
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


def _query_graph(graph_db: Path, file_path: str) -> tuple:
    """Count callers and tests for all functions in a file."""
    conn = sqlite3.connect(str(graph_db))
    conn.execute("PRAGMA busy_timeout=1000")

    # Try both absolute and relative path
    callers = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS' AND target_qualified IN "
        "(SELECT qualified_name FROM nodes WHERE file_path = ?)",
        (file_path,)
    ).fetchone()[0]

    tests = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'TESTED_BY' AND "
        "source_qualified IN (SELECT qualified_name FROM nodes WHERE file_path = ?)",
        (file_path,)
    ).fetchone()[0]

    conn.close()
    return (callers, tests)


def _query_cairn(cairn_db: Path, file_path: str) -> list:
    """Find high-confidence correction/decision memories tagged to file."""
    conn = sqlite3.connect(str(cairn_db))
    conn.execute("PRAGMA busy_timeout=1000")

    rows = conn.execute(
        "SELECT type, content, updated_at FROM memories "
        "WHERE type IN ('correction', 'decision') "
        "AND deleted_at IS NULL "
        "AND confidence >= 0.7 "
        "AND associated_files LIKE ? "
        "ORDER BY updated_at DESC LIMIT 2",
        (f'%{file_path}%',)
    ).fetchall()

    conn.close()
    return [{'type': r[0], 'content': r[1], 'updated_at': r[2]} for r in rows]


def _format_footer(callers: int, tests: int, memories: list) -> Optional[str]:
    """Build footer string, max 200 chars."""
    if callers == 0 and tests == 0 and not memories:
        return None

    parts = [f'{callers} callers', f'{tests} tests']

    for m in memories[:2]:
        try:
            dt = datetime.fromisoformat(m['updated_at'].replace('Z', '+00:00'))
            days = (datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)).days
        except (ValueError, AttributeError):
            days = 0
        snippet = m['content'][:60]
        label = m['type']
        parts.append(f'{label} {days}d ago: "{snippet}"')

    footer = '[cairn-graph: ' + ' · '.join(parts) + ']'

    if len(footer) > 200:
        footer = footer[:197] + '...]'

    return footer


def generate_footer(command: str, cwd: str) -> Optional[str]:
    """Generate cairn-graph footer for a code-file read command.
    Best-effort: returns None on any error. Never raises.
    """
    try:
        file_path = _extract_source_file(command, cwd)
        if file_path is None:
            return None

        # Walk up from the file itself, not cwd (cwd may be the Claude
        # Code project dir, not the repo containing the file)
        graph_db = _find_graph_db(str(Path(file_path).parent))
        if graph_db is None:
            return None

        callers, tests = _query_graph(graph_db, file_path)

        memories = []
        cairn_db = _find_cairn_db()
        if cairn_db:
            memories = _query_cairn(cairn_db, file_path)

        return _format_footer(callers, tests, memories)
    except Exception:
        return None
