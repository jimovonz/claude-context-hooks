#!/usr/bin/env python3
"""
Configuration for Claude Code context hooks.
Edit these values to tune hook behavior.
"""

__version__ = "2.0.0"

from pathlib import Path

# Cache settings
CACHE_DIR = Path.home() / '.claude' / 'cache'
CACHE_MAX_AGE_MINUTES = 60

# Output size thresholds (bytes) - outputs larger than this get cached
# Small outputs (<8KB) pass through directly - caching overhead exceeds benefit
BASH_THRESHOLD = 8000   # ~2k tokens
GLOB_THRESHOLD = 8000   # ~2k tokens
GREP_THRESHOLD = 8000   # ~2k tokens
READ_THRESHOLD = 25000  # ~6k tokens

# Learned patterns settings
PATTERNS_EXPIRY_DAYS = 30

# Metrics logging (set to True to enable)
METRICS_ENABLED = False

# =============================================================================
# Context Monitor Settings
# =============================================================================

# Enable/disable context usage warnings
CONTEXT_MONITOR_ENABLED = True

# Claude's context window size (tokens)
CONTEXT_MAX_TOKENS = 1000000

# Warn at these percentage thresholds (only warns once per threshold per session)
CONTEXT_WARN_THRESHOLDS = [70, 80, 90]

# Estimation parameters
CONTEXT_CHARS_PER_TOKEN = 2.5  # Fallback when tiktoken not installed (empirically ~2.4)
CONTEXT_OVERHEAD_TOKENS = 45000  # System prompt (~20k) + tools (~15k) + hidden (~10k)
CONTEXT_MESSAGE_MULTIPLIER = 1.5  # Claude counts more than extracted text (structure, metadata)

# =============================================================================
# CCM (Content Cache Manager) Settings
# =============================================================================

# Enable CCM durable cache (content-addressed, compressed, with pinning)
CCM_ENABLED = True

# Compression method: 'auto' (zstd > gzip > none), 'zstd', 'gzip', or 'none'
CCM_COMPRESSION = 'auto'

# Default pin level for content cached via pin directives
CCM_DEFAULT_PIN_LEVEL = 'soft'

# Cache pruning defaults
CCM_PRUNE_MAX_AGE_DAYS = 30      # Delete unpinned items older than this
CCM_PRUNE_MAX_SIZE_MB = 500      # Max total cache size

# Stub threshold: tool_results larger than this get stubbed during purge
CCM_STUB_THRESHOLD_BYTES = 5000

# Recent lines window: tool_results within this many lines of end are kept
CCM_RECENT_LINES_WINDOW = 20
