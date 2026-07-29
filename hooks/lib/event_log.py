"""
Append-only JSONL event log for cch-gain.py.

Each hook writes one row per significant event (cache stub emitted, deny
issued, guard warning, batch run). cch-gain.py aggregates these and
hooks/retrieval.log into a token report.

Best-effort. Never raises — logging failure must never break a hook.

Two properties this needs and did not have:

  * Atomic appends. Up to `--jobs` cch-batch workers append concurrently;
    a buffered text write can tear and leave a half row (one corrupt line
    was observed in 62k). A single os.write() of the whole row to an
    O_APPEND fd is atomic for rows this size on Linux.

  * Session attribution. CCH_SESSION_ID is set on every wrapped command
    but was never recorded, which made the project's own acceptance
    signal — ~1-2 corrections per session — impossible to compute from
    62k events. Every row now carries `sid`.
"""

import json
import os
from datetime import datetime
from pathlib import Path

EVENTS_LOG = Path.home() / '.claude' / 'cache' / 'ccm' / 'events.jsonl'


def current_session() -> str:
    """Session id for the current command, or '' when unattributed."""
    return os.environ.get('CCH_SESSION_ID', '')[:36]


def log_event(event: str, **fields) -> None:
    try:
        EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        if not fields.get('sid'):
            fields['sid'] = current_session()
        row = {
            'ts': datetime.now().isoformat(timespec='seconds'),
            'event': event,
            **fields,
        }
        line = (json.dumps(row, separators=(',', ':')) + '\n').encode('utf-8')
        fd = os.open(str(EVENTS_LOG),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass
