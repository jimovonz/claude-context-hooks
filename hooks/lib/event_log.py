"""
Append-only JSONL event log for cch-gain.py.

Each hook writes one row per significant event (cache stub emitted,
deny issued). cch-gain.py aggregates these and the existing
hooks/retrieval.log into a token-savings report.

Best-effort. Never raises — logging failure must never break a hook.
"""

import json
from datetime import datetime
from pathlib import Path

EVENTS_LOG = Path.home() / '.claude' / 'cache' / 'ccm' / 'events.jsonl'


def log_event(event: str, **fields) -> None:
    try:
        EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            'ts': datetime.now().isoformat(timespec='seconds'),
            'event': event,
            **fields,
        }
        with open(EVENTS_LOG, 'a') as f:
            f.write(json.dumps(row) + '\n')
    except Exception:
        pass
