# claude-context-hooks

Lightweight Claude Code hook layer that minimises tool-output context cost
by routing all data interaction through a single Bash data path. Built-in
`Read`, `Grep`, `Glob`, `WebFetch`, `Edit`, `Write`, and `NotebookEdit`
are blocked at the hook layer; `Read` of multimodal extensions is the only
allowed built-in path. Bash output is RTK-compressed and large residuals
are cached for selective querying via `ccm-get.py`. Edits and writes go
through `cch-edit` and `cch-write` helpers (Bash-routed) so the
read-before-edit guard never fires.

Coexists with [RTK](https://github.com/rtk-ai/rtk) and
[Cairn](https://github.com/jimovonz/cairn).

## Architecture in one breath

Block built-in tools → all data goes through Bash → RTK compresses Bash →
wrapper caches residual large output → `ccm-get.py` retrieves selectively.
Edits and writes routed through `cch-edit` / `cch-write` helpers so the
read-before-edit guard never fires. Cairn untouched.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full design — including
why blocking built-in writes matters (the read-before-edit guard would
otherwise pull the full edit-target file into context, defeating the
cache wrapper for the very files being edited).

## Install

```
git clone https://github.com/jimovonz/claude-context-hooks
cd claude-context-hooks
python3 install.py
```

The installer symlinks hooks and helpers into `~/.claude/hooks/`,
registers PreToolUse entries in `~/.claude/settings.json` (appending
after any existing PreToolUse:Bash hook so RTK's rewrite still fires
first), and warns if `rtk` is not on PATH. Updates: `git pull`
(symlinks track the working copy).

Then paste [`docs/CLAUDE_MD_SNIPPET.md`](docs/CLAUDE_MD_SNIPPET.md) into
your `~/.claude/CLAUDE.md` so the model knows how to route.

`python3 install.py --remove` cleans up symlinks and settings entries.
`python3 install.py --check` runs pre-flight checks only.

## Components

### PreToolUse hooks (block + redirect)

| Path                                | Role                                                            |
| ----------------------------------- | --------------------------------------------------------------- |
| `hooks/intercept-bash.py`           | Wraps command in `cache-wrap.py` (after RTK rewrite)            |
| `hooks/intercept-read.py`           | Multimodal-only allowlist; everything else denied with redirect |
| `hooks/intercept-grep.py`           | Block + redirect to `rg` via Bash                               |
| `hooks/intercept-glob.py`           | Block + redirect to `fd` via Bash                               |
| `hooks/intercept-webfetch.py`       | Block + redirect to `curl` via Bash                             |
| `hooks/intercept-edit.py`           | Block + redirect to `cch-edit` via Bash                         |
| `hooks/intercept-write.py`          | Block + redirect to `cch-write` via Bash                        |
| `hooks/intercept-notebookedit.py`   | Block + redirect to `cch-edit` / `jq` / `nbformat` via Bash     |

### Bash-routed helpers

| Path                          | Role                                                         |
| ----------------------------- | ------------------------------------------------------------ |
| `hooks/cache-wrap.py`         | Runs the inner command, caches + stubs output above threshold |
| `hooks/ccm-get.py`            | Filtered cache retrieval (`--grep` / `--head` / `--tail` / `--lines`) |
| `hooks/cch-edit.py`           | Literal-string edit: exact match, uniqueness check, atomic write, unified diff |
| `hooks/cch-write.py`          | Atomic file write from stdin; creates parent directories     |
| `hooks/lib/ccm_cache.py`      | Content-addressable cache (BLAKE2s, zstd/gzip)               |

## Environment variables

| Var                     | Effect                                                  |
| ----------------------- | ------------------------------------------------------- |
| `CCH_DISABLE=1`         | All hooks pass through (debug escape hatch)             |
| `CCH_CACHE_THRESHOLD`   | Bytes threshold for caching Bash output (default 8000)  |

## Tests

```
python3 -m pytest tests/
```

## Dependencies

User prerequisites (not auto-installed):

- Python 3.10+
- Claude Code with `hookSpecificOutput.updatedInput` support
- [RTK](https://github.com/rtk-ai/rtk) — for command compression
- [Cairn](https://github.com/jimovonz/cairn) — for cross-session memory
- Optional: `zstandard` (better cache compression), `tiktoken` (accurate
  token counts in stubs)

## License

[MIT](LICENSE)
