#!/usr/bin/env python3
"""
Shared library for Claude Code hooks.
Import this module at the start of each hook script.
"""

import json
import os
import sys
import subprocess
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple

# Load configuration
HOOKS_DIR = Path(os.environ.get('HOOKS_DIR', Path.home() / '.claude' / 'hooks'))
CONFIG_FILE = HOOKS_DIR / 'config.py'

# Defaults (can be overridden in config.py)
CACHE_DIR = Path.home() / '.claude' / 'cache'
CACHE_MAX_AGE_MINUTES = 60
BASH_THRESHOLD = 8000
GLOB_THRESHOLD = 8000
GREP_THRESHOLD = 8000
READ_THRESHOLD = 25000
PATTERNS_EXPIRY_DAYS = 30
METRICS_ENABLED = False

# CCM defaults
CCM_ENABLED = True
CCM_COMPRESSION = 'auto'
CCM_DEFAULT_PIN_LEVEL = 'soft'
CCM_STUB_THRESHOLD_BYTES = 5000

# Session state for deduplication and adaptive verbosity
SESSION_STATE_DIR = CACHE_DIR / 'session_state'

# Load config if exists
if CONFIG_FILE.exists():
    _config = {}
    exec(CONFIG_FILE.read_text(), _config)
    for _key in ['CACHE_DIR', 'CACHE_MAX_AGE_MINUTES', 'BASH_THRESHOLD',
                 'GLOB_THRESHOLD', 'GREP_THRESHOLD', 'READ_THRESHOLD',
                 'PATTERNS_EXPIRY_DAYS', 'METRICS_ENABLED',
                 'CCM_ENABLED', 'CCM_COMPRESSION', 'CCM_DEFAULT_PIN_LEVEL',
                 'CCM_STUB_THRESHOLD_BYTES']:
        if _key in _config:
            globals()[_key] = _config[_key]


def init_cache() -> None:
    """Initialize cache directory and clean old files."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now().timestamp() - (CACHE_MAX_AGE_MINUTES * 60)
    for f in CACHE_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


def check_passthrough() -> None:
    """Check for passthrough mode (bypass all hooks)."""
    if os.environ.get('CLAUDE_HOOKS_PASSTHROUGH') == '1':
        print('{}')
        sys.exit(0)


def _get_session_id(transcript_path: str) -> str:
    """Extract session ID from transcript path."""
    if not transcript_path:
        return 'unknown'
    # Transcript path is like: ~/.claude/projects/.../session_id.jsonl
    return Path(transcript_path).stem


def _get_session_state_path(session_id: str) -> Path:
    """Get path to session state file."""
    SESSION_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_STATE_DIR / f'{session_id}.json'


def _load_session_state(session_id: str) -> dict:
    """Load session state, cleaning up old sessions."""
    # Clean old session state files (>24h)
    try:
        cutoff = datetime.now().timestamp() - 86400
        for f in SESSION_STATE_DIR.glob('*.json'):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except (OSError, FileNotFoundError):
        pass

    state_path = _get_session_state_path(session_id)
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {'seen_keys': [], 'guidance_shown': False}


def _save_session_state(session_id: str, state: dict) -> None:
    """Save session state."""
    try:
        state_path = _get_session_state_path(session_id)
        state_path.write_text(json.dumps(state))
    except OSError:
        pass


def is_key_seen(transcript_path: str, cache_key: str) -> bool:
    """Check if a cache key has been seen in this session."""
    session_id = _get_session_id(transcript_path)
    state = _load_session_state(session_id)
    return cache_key in state.get('seen_keys', [])


def mark_key_seen(transcript_path: str, cache_key: str) -> bool:
    """Mark a cache key as seen. Returns True if this was first time."""
    session_id = _get_session_id(transcript_path)
    state = _load_session_state(session_id)
    seen_keys = state.get('seen_keys', [])
    first_time = cache_key not in seen_keys
    if first_time:
        seen_keys.append(cache_key)
        state['seen_keys'] = seen_keys
        _save_session_state(session_id, state)
    return first_time


def should_show_guidance(transcript_path: str) -> bool:
    """Check if full guidance should be shown (first cache in session)."""
    session_id = _get_session_id(transcript_path)
    state = _load_session_state(session_id)
    return not state.get('guidance_shown', False)


def mark_guidance_shown(transcript_path: str) -> None:
    """Mark that guidance has been shown for this session."""
    session_id = _get_session_id(transcript_path)
    state = _load_session_state(session_id)
    state['guidance_shown'] = True
    _save_session_state(session_id, state)


def is_subagent(transcript_path: str, tool_use_id: str) -> bool:
    """Check if current call is from a subagent."""
    if not transcript_path:
        return False
    transcript_dir = Path(transcript_path).parent

    # Quick check: any agent files exist?
    # Agent files can be in transcript_dir (old structure) or subagents/ subdirectory (new structure)
    agent_files = list(transcript_dir.glob('agent-*.jsonl'))

    # Also check subagents/ subdirectory (Claude Code 2.1.x structure)
    # The session directory is named after the session ID (without .jsonl)
    session_dir = transcript_dir / Path(transcript_path).stem
    subagents_dir = session_dir / 'subagents'
    if subagents_dir.exists():
        agent_files.extend(subagents_dir.glob('agent-*.jsonl'))

    if not agent_files:
        return False

    search_pattern = f'"id":"{tool_use_id}"'

    for agent_file in agent_files:
        try:
            # Only check tail of file - recent tool calls are at the end
            # Tool use ID appears when assistant generates the call,
            # which is recent relative to PreToolUse hook firing
            file_size = agent_file.stat().st_size
            read_size = min(file_size, 64 * 1024)  # Last 64KB

            with open(agent_file, 'rb') as f:
                if file_size > read_size:
                    f.seek(-read_size, 2)  # Seek from end
                content = f.read().decode('utf-8', errors='ignore')

            if search_pattern in content:
                return True
        except OSError:
            pass
    return False


def allow_if_subagent(transcript_path: str, tool_use_id: str) -> None:
    """Allow subagent through without interception."""
    if is_subagent(transcript_path, tool_use_id):
        json_pass()
        sys.exit(0)


def cache_output(content: str) -> str:
    """Cache content to file, return UUID (legacy format)."""
    file_uuid = uuid.uuid4().hex[:8]
    cache_file = CACHE_DIR / file_uuid
    cache_file.write_text(content)
    return file_uuid


def cache_output_ccm(
    content: str,
    tool_name: str = 'unknown',
    exit_code: int = 0,
    command: str = '',
    cwd: str = '',
    session_path: str = '',
    pin_level: str = 'none',
    pin_reason: str = ''
) -> str:
    """
    Cache content using CCM durable storage.

    Returns cache key (sha256:...) if CCM enabled, else legacy UUID.
    """
    if not CCM_ENABLED:
        return cache_output(content)

    try:
        from lib.ccm_cache import init_ccm_cache, store_content

        init_ccm_cache(CACHE_DIR)

        source = {
            'tool_name': tool_name,
            'exit_code': exit_code,
            'command': command,
            'cwd': cwd,
            'session_path': session_path,
        }

        key = store_content(
            content,
            source=source,
            pin_level=pin_level,
            pin_reason=pin_reason
        )
        return key
    except ImportError:
        # Fallback to legacy cache
        return cache_output(content)


def build_ccm_cache_response(
    key: str,
    lines: int,
    size: int,
    exit_code: int,
    original: str
) -> str:
    """Build cache response message with CCM stub format."""
    if key.startswith('sha256:') or key.startswith('b2s:'):
        # CCM format - strip prefix for display
        hex_key = key[7:] if key.startswith('sha256:') else key[4:]
        try:
            from lib.ccm_cache import build_ccm_stub, get_metadata

            meta = get_metadata(key)
            pin_level = meta.get('pinned', {}).get('level', 'none') if meta else 'none'

            stub = build_ccm_stub(key, size, lines, exit_code, pin_level)
            # Compact retrieval line with filter hint
            return f"""{stub}
Retrieve: ccm-get.py {hex_key} [--grep PATTERN] [--head N] [--lines N-M]"""
        except ImportError:
            pass

    # Fallback to legacy format
    return build_cache_response(key, lines, size, exit_code, original)


def get_size_category(size: int) -> tuple[str, str, str]:
    """Get size category, guidance, and recommended action.

    Returns (category, guidance, action_prompt) tuple.
    Categories defined in CLAUDE.md for consistent model behavior.
    """
    tokens_k = size / 4000
    context_pct = tokens_k / 200 * 100

    if size < 25000:
        # SMALL: 8-25KB (~2-6k tokens)
        return (
            "SMALL",
            f"{tokens_k:.0f}k tokens",
            "Filter or full retrieval OK"
        )

    elif size < 50000:
        # MEDIUM: 25-50KB (~6-12k tokens)
        return (
            "MEDIUM",
            f"{tokens_k:.0f}k tokens ({context_pct:.0f}% context)",
            "Use --grep/--head/--lines to filter. Full only if editing."
        )

    elif size < 100000:
        # LARGE: 50-100KB (~12-25k tokens)
        return (
            "LARGE",
            f"{tokens_k:.0f}k tokens ({context_pct:.0f}% context)",
            "FILTER REQUIRED: --grep PATTERN or --lines N-M. Full retrieval only for editing."
        )

    else:
        # MASSIVE: >100KB (~25k+ tokens)
        return (
            "MASSIVE",
            f"{tokens_k:.0f}k tokens ({context_pct:.0f}% context)",
            "MUST FILTER. Full retrieval will trigger compaction. Use --grep/--head/--tail."
        )


def build_retrieval_guidance(size: int, lines: int, verbose: bool = True) -> str:
    """Build size-proportional retrieval guidance with category and options.

    Args:
        size: Content size in bytes
        lines: Number of lines
        verbose: If True, include full OPTIONS text. If False, minimal format.
    """
    category, guidance, options = get_size_category(size)
    if verbose:
        return f"\ncategory: {category}\nguidance: {guidance}\n{options}"
    else:
        # Minimal format for subsequent stubs
        return f"\ncategory: {category}"


def build_duplicate_stub(cache_key: str) -> str:
    """Build minimal stub for duplicate cache reference.

    Returns something like: [CCM: sha256:abc123... - see earlier]
    """
    # Truncate key for display
    if len(cache_key) > 20:
        short_key = cache_key[:20] + '...'
    else:
        short_key = cache_key
    return f"[CCM: {short_key} - see earlier in conversation]"


def json_block(reason: str, exit_code: int = None) -> None:
    """Output JSON to block tool execution with reason."""
    if exit_code is not None and exit_code == 0:
        reason = f"None - {reason}"
    print(json.dumps({"decision": "block", "reason": reason}))


def json_pass() -> None:
    """Output JSON to allow tool execution (pass through)."""
    print('{}')


def build_cache_response(file_uuid: str, lines: int, size: int, exit_code: int, original: str) -> str:
    """Build cache response message (minimal)."""
    # Strip prefix if present for cleaner display
    if file_uuid.startswith('sha256:'):
        hex_key = file_uuid[7:]
    elif file_uuid.startswith('b2s:'):
        hex_key = file_uuid[4:]
    else:
        hex_key = file_uuid
    return f"""Cached ({lines} lines, {size} bytes, exit {exit_code}).
Key: {hex_key}
Original: {original}

Options: Task agent (summarize or full content), or paginate with offset/limit.

Retrieve: ccm-get.py {hex_key} [--grep PATTERN] [--head N] [--lines N-M]"""


def log_metric(tool: str, action: str, size: int = 0) -> None:
    """Log metrics (if enabled)."""
    if not METRICS_ENABLED:
        return
    timestamp = datetime.now().isoformat()
    log_file = HOOKS_DIR / 'metrics.log'
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} {tool} {action} {size}\n")


def parse_hook_input() -> dict:
    """Parse hook input from stdin."""
    return json.load(sys.stdin)


def get_common_fields(input_data: dict) -> Tuple[str, str, str, str]:
    """Extract common fields from hook input."""
    tool = input_data.get('tool_name', '')
    transcript_path = input_data.get('transcript_path', '')
    tool_use_id = input_data.get('tool_use_id', '')
    cwd = input_data.get('session', {}).get('cwd', '')
    return tool, transcript_path, tool_use_id, cwd


def run_command(cmd: str, cwd: Optional[str] = None, timeout: int = 120) -> Tuple[str, int]:
    """Run a shell command and return (output, exit_code)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd if cwd and Path(cwd).is_dir() else None,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        output = result.stdout + result.stderr
        return output, result.returncode
    except subprocess.TimeoutExpired:
        return "Command timed out", 124
    except Exception as e:
        return str(e), 1


# Command classification cache
COMMAND_CACHE_FILE = HOOKS_DIR / 'command-cache.json'
PROBE_TIMEOUT = 2.0  # Seconds to wait before assuming command is interactive


def load_command_cache() -> dict:
    """Load cached command classifications."""
    if not COMMAND_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(COMMAND_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_command_cache(cache: dict) -> None:
    """Save command classification cache."""
    try:
        COMMAND_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass


def classify_with_haiku(cmd: str, pattern: str) -> Optional[dict]:
    """Ask Haiku to classify a command. Returns {"interactive": 0|1, "large_output": 0|1}."""
    prompt = f'''Classify this Linux command. Reply ONLY with JSON, no other text.
{{"interactive": 0 or 1, "large_output": 0 or 1}}

interactive=1 if command may EVER prompt for user input at ANY point during execution, including:
- OAuth/authentication flows (browser opens, device codes)
- Password or passphrase prompts
- Confirmation prompts (Y/n, yes/no, Continue?)
- Interactive installers or setup wizards
- Commands that wait for user input before proceeding
- Deployment confirmations at the end of builds

large_output=1 if command typically produces more than 50 lines of output

Command: {cmd}
Pattern: {pattern}'''

    try:
        result = subprocess.run(
            ['claude', '-p', prompt, '--model', 'haiku'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            # Parse JSON from response
            response = result.stdout.strip()
            # Handle case where response has extra text
            import re
            match = re.search(r'\{[^}]+\}', response)
            if match:
                return json.loads(match.group())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        pass
    return None


def get_command_classification(cmd: str) -> Optional[dict]:
    """Get classification for a command, using cache or Haiku."""
    pattern = extract_command_pattern(cmd)
    if not pattern:
        return None

    # Check cache
    cache = load_command_cache()
    if pattern in cache:
        return cache[pattern]

    # Ask Haiku
    classification = classify_with_haiku(cmd, pattern)
    if classification:
        # Cache result
        cache[pattern] = classification
        save_command_cache(cache)
        return classification

    return None


def extract_command_pattern(cmd: str) -> Optional[str]:
    """Extract a generalizable pattern from a command.

    Examples:
        'gh auth refresh -h github.com' -> 'gh auth'
        'ssh user@host' -> 'ssh'
        'python3 script.py' -> None (too generic)
    """
    import shlex
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()

    if not parts:
        return None

    base = parts[0]

    # Skip overly generic commands
    generic = {'python', 'python3', 'node', 'bash', 'sh', 'ruby', 'perl'}
    if base in generic:
        return None

    # For multi-level commands, include subcommand
    if len(parts) > 1 and not parts[1].startswith('-'):
        # Commands with subcommands: gh auth, git credential, docker login, etc.
        multi_level = {'gh', 'git', 'docker', 'kubectl', 'aws', 'gcloud', 'az', 'npm', 'yarn'}
        if base in multi_level:
            return f"{base} {parts[1]}"

    return base


def is_cached_interactive(cmd: str) -> Optional[bool]:
    """Check if command is cached as interactive. Returns None if not cached."""
    pattern = extract_command_pattern(cmd)
    if not pattern:
        return None

    cache = load_command_cache()
    if pattern in cache:
        return cache[pattern].get('interactive', 0) == 1

    return None


def is_cached_large_output(cmd: str) -> Optional[bool]:
    """Check if command is cached as large output. Returns None if not cached."""
    pattern = extract_command_pattern(cmd)
    if not pattern:
        return None

    cache = load_command_cache()
    if pattern in cache:
        return cache[pattern].get('large_output', 0) == 1

    return None


def learn_command_classification(cmd: str, interactive: bool = False, large_output: bool = False) -> None:
    """Learn a command classification from runtime behavior."""
    pattern = extract_command_pattern(cmd)
    if not pattern:
        return

    cache = load_command_cache()
    # Only update if not already cached (don't override Haiku's judgment with runtime guess)
    if pattern not in cache:
        cache[pattern] = {
            'interactive': 1 if interactive else 0,
            'large_output': 1 if large_output else 0,
            'source': 'learned'
        }
        save_command_cache(cache)


# Patterns that indicate interactive prompt in output
INTERACTIVE_OUTPUT_PATTERNS = [
    r'\[Y/n\]', r'\[y/N\]', r'\[yes/no\]',
    r'Continue\?', r'Proceed\?', r'Are you sure',
    r'Enter password', r'Enter passphrase', r'Password:',
    r'one-time code', r'Press Enter', r'Press any key',
    r'Login with', r'Waiting for .* input',
    r'Do you want to', r'Would you like to',
]
import re as _re
_INTERACTIVE_OUTPUT_RE = _re.compile('|'.join(INTERACTIVE_OUTPUT_PATTERNS), _re.IGNORECASE)


def probe_command(cmd: str, cwd: Optional[str] = None,
                  full_timeout: int = 120) -> Tuple[Optional[str], int, bool]:
    """
    Run command with stdin closed and continuous monitoring for interactive patterns.

    Returns (output, exit_code, is_interactive).
    If is_interactive=True, the command was killed and should be passed through.
    """
    import select

    work_cwd = cwd if cwd and Path(cwd).is_dir() else None

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=work_cwd,
            stdin=subprocess.DEVNULL,  # Close stdin - interactive commands will fail/hang
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        return str(e), 1, False

    output_chunks = []
    start_time = datetime.now()
    last_output_time = start_time

    def check_for_interactive(text: str) -> bool:
        """Check if output contains interactive patterns."""
        return bool(_INTERACTIVE_OUTPUT_RE.search(text))

    try:
        # Main execution loop with continuous monitoring
        while True:
            elapsed = (datetime.now() - start_time).total_seconds()

            # Check if process finished
            ret = proc.poll()
            if ret is not None:
                stdout, stderr = proc.communicate(timeout=1)
                output_chunks.append(stdout)
                output_chunks.append(stderr)
                return ''.join(output_chunks), ret, False

            # Read available output
            new_output = False
            if hasattr(select, 'select'):
                readable, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)
                for stream in readable:
                    chunk = stream.read(4096) if stream else ''
                    if chunk:
                        output_chunks.append(chunk)
                        new_output = True
                        last_output_time = datetime.now()

                        # Check new output for interactive patterns
                        if check_for_interactive(chunk):
                            proc.terminate()
                            try:
                                proc.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            return ''.join(output_chunks), -1, True

            # Timeout checks
            if elapsed >= full_timeout:
                proc.kill()
                proc.communicate()
                return ''.join(output_chunks) + "\nCommand timed out", 124, False

            # If no output for PROBE_TIMEOUT seconds early in execution, likely hung
            time_since_output = (datetime.now() - last_output_time).total_seconds()
            if elapsed < 10 and time_since_output >= PROBE_TIMEOUT:
                partial = ''.join(output_chunks)
                if len(partial.strip()) < 50:
                    # No meaningful output, likely waiting for input
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return partial, -1, True

    except Exception as e:
        try:
            proc.kill()
        except:
            pass
        return str(e), 1, False
