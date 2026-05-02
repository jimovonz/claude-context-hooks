# System spec: claude-context-hooks + cairn-graph

Forward spec for the unified token-efficient Claude Code environment.
DESIGN.md remains the architectural rationale for CCH v2; this document
specifies the full intended end-state including the cairn-graph
integration discussed 2026-05-02.

## 1. Purpose

Reduce per-task token cost on medium-to-large repos to roughly constant
in repo size by:

- Routing all data interaction through a single Bash data path so RTK
  compression and CCH caching apply uniformly.
- Addressing code at symbol granularity (function / class) rather than
  file granularity, via a structural graph index.
- Slicing reads to the minimum correct unit (function body, line range)
  rather than whole files.
- Editing without the read-before-edit token tax via `cch-edit` /
  `cch-write` helpers.
- Capturing decisions and corrections in cairn so future sessions
  start with priors instead of re-deriving.

Three coordinated layers, each owned by a different project:

| Layer    | Project                  | Responsibility                                    |
| -------- | ------------------------ | ------------------------------------------------- |
| Compress | RTK                      | Per-command output compression on Bash            |
| Route    | claude-context-hooks     | Single Bash data path + cache wrapper + helpers   |
| Memory   | cairn (+ cairn-graph)    | Persistent decisions + structural code navigation |

## 2. Architecture

```
                  user prompt
                       │
                       ▼
              UserPromptSubmit ── cairn (proactive context)
                       │
                       ▼
              Claude Code (model + harness)
                       │ tool calls
                       ▼
   ┌─────────────────── PreToolUse ──────────────────────────┐
   │ Read / Edit / Write / NotebookEdit / Grep / Glob /      │
   │ WebFetch    → CCH (deny + redirect-to-Bash suggest)     │
   │ Bash        → RTK rewrite → CCH cache wrapper           │
   └─────────────────────────────────────────────────────────┘
                       │
                       ▼
                Bash subprocess
   (cat / sed / rg / fd / curl / cch-edit / cch-write /
    cairn-graph / ccm-get / git / pytest / ...)
                       │
                       ▼
              tool_result (inline if small,
              [CCM_CACHED] stub if > threshold)
                       │
                       ▼
                    Stop ── cairn (memory capture)
```

Multimodal Read (.png / .jpg / .pdf / .svg / .ipynb / etc.) is the only
allowed built-in path because Bash has no equivalent. Every other path
funnels through Bash.

## 3. Component inventory

### 3.1 External dependencies (user installs separately)

| Component | Purpose                              | Install                       |
| --------- | ------------------------------------ | ----------------------------- |
| RTK       | Bash command compression             | https://github.com/rtk-ai/rtk |
| cairn     | Persistent memory + injection hooks  | https://github.com/jimovonz/cairn |
| code-review-graph (`crg`) | Tree-sitter call graph indexer | `pip install code-review-graph` |

### 3.2 claude-context-hooks (this project)

Hooks (live in `hooks/`):

| File                       | Hook              | Job                                              |
| -------------------------- | ----------------- | ------------------------------------------------ |
| `intercept-bash.py`        | PreToolUse:Bash   | Cache wrapper (chains after RTK)                 |
| `intercept-read.py`        | PreToolUse:Read   | Deny + multimodal allowlist                      |
| `intercept-grep.py`        | PreToolUse:Grep   | Deny + redirect to `rg`                          |
| `intercept-glob.py`        | PreToolUse:Glob   | Deny + redirect to `fd` / `find`                 |
| `intercept-webfetch.py`    | PreToolUse:WebFetch | Deny + redirect to `curl`                      |
| `intercept-edit.py`        | PreToolUse:Edit   | Deny + redirect to `cch-edit.py`                 |
| `intercept-write.py`       | PreToolUse:Write  | Deny + redirect to `cch-write.py`                |
| `intercept-notebookedit.py`| PreToolUse:NotebookEdit | Deny + redirect to `cch-edit.py` / `jq`    |
| `lib/ccm_cache.py`         | (library)         | Cache key + slice retrieval primitives           |

Helpers (live in `hooks/`, symlinked into `~/.local/bin/`):

| File           | Purpose                                                      |
| -------------- | ------------------------------------------------------------ |
| `cch-edit.py`  | Literal-match unique-string replacement, atomic, prints diff |
| `cch-write.py` | Atomic full-file write from stdin                            |
| `ccm-get.py`   | Slice retrieval from cached Bash output                      |
| `cch-gain.py`  | Token-savings analytics (`--dist`, `--retrieval`, `--history`) |

### 3.3 cairn-graph (new component, lives in cairn repo)

Per memory `2336462276880`:

- `cairn/graph.py` — thin query layer over `<repo>/.code-review-graph/graph.db`.
- New subcommands on `cairn/query.py`:

| Subcommand                         | Returns                                                  |
| ---------------------------------- | -------------------------------------------------------- |
| `--location SYMBOL`                | `path:start-end`                                         |
| `--callers SYMBOL`                 | Table of callers with `path:line:caller_symbol`          |
| `--callees SYMBOL`                 | Table of callees with `path:line:callee_symbol`          |
| `--tests SYMBOL`                   | TESTED_BY edges → test fn locations                      |
| `--summary`                        | Repo-level: entry points, top fan-in, untested public funcs |
| `--knowledge SYMBOL`               | Cairn memories tagged to file/symbol (knowledge join)    |
| `--context-pack SYMBOL`            | Function body + callers + tests + decisions, sliced      |
| `--impact SYMBOL`                  | Blast-radius summary `callers:N tests:M files:F`         |

Output shape: small text rows (sub-2 KB typical) so they bypass the
cache wrapper and remain inline-visible.

### 3.4 Integration points (new)

Three narrow features that turn coexistence into synergy:

- **`ccm-get.py --symbol NAME`** — extract a function body from a cached
  blob using graph-resolved line ranges. Closes the only remaining gap
  in the cache flow (cached file → specific function).
- **`cch-edit.py` opportunistic impact line** — after the unified diff,
  print one line `callers:N tests:M` if the edited region maps to a
  known function. Sub-millisecond on cache hit; no blocking; no new
  flag.
- **`cch-edit.py` cache invalidation signal** — on successful edit,
  bump mtime tracking for the path so the next graph query reparses
  just that file. Solves the freshness-cadence open question without
  a watcher daemon.

## 4. Routing policy (canonical)

This table lives verbatim in `~/.claude/CLAUDE.md` between sentinel
markers, written by `install.py`.

| Want                                   | Use                                                                   |
| -------------------------------------- | --------------------------------------------------------------------- |
| Inspect a text file                    | `cat PATH` / `head -n N PATH` / `sed -n 'A,Bp' PATH`                  |
| Search file contents                   | `rg -n PATTERN PATH` (with `-C`, `--type`, `-l` as needed)            |
| List files                             | `fd PATTERN PATH` / `find PATH -name 'GLOB' -type f`                  |
| Fetch a URL                            | `curl -sSL URL` (pipe to `rtk html` for HTML→markdown)                |
| **Locate a symbol**                    | **`cairn-graph --location SYMBOL`**                                   |
| **Callers / callees / tests of a symbol** | **`cairn-graph --callers SYMBOL` / `--callees` / `--tests`**       |
| **Repo orientation**                   | **`cairn-graph --summary`**                                           |
| **Past decisions about a symbol**      | **`cairn-graph --knowledge SYMBOL`**                                  |
| Replace a literal string in a file     | `cch-edit.py PATH 'old' 'new'`                                        |
| Replace multi-line content             | `cch-edit.py PATH --old-file F1 --new-file F2`                        |
| Write a new or full file               | `cch-write.py PATH << 'EOF' ... EOF`                                  |
| Edit a notebook                        | `cch-edit.py PATH 'old_source' 'new_source'`                          |
| Retrieve a slice of cached output      | `ccm-get.py KEY --grep PAT` / `--head N` / `--tail N` / `--lines A-B` / `--symbol NAME` |

The four bolded rows are the navigate verb introduced by the graph
integration. They sit alongside find / inspect / edit, completing the
verb set.

## 5. Installation

### 5.1 One-time global setup

Run in this order; each step is independently re-runnable.

1. **RTK**
   ```
   cargo install rtk-cli            # or release binary to ~/.local/bin/rtk
   rtk --version                    # verify
   ```

2. **cairn**
   ```
   git clone https://github.com/jimovonz/cairn ~/Projects/cairn
   cd ~/Projects/cairn && python3 install.py
   python3 cairn/query.py --stats   # verify
   ```

3. **code-review-graph**
   ```
   pip install --user code-review-graph
   crg --help                       # verify
   ```

4. **claude-context-hooks**
   ```
   git clone https://github.com/jimovonz/claude-context-hooks \
     ~/Projects/claude-context-hooks
   cd ~/Projects/claude-context-hooks && python3 install.py
   ```

   `install.py` does (per memory `2336462276666`):
   - Symlinks 9+ hook scripts into `~/.claude/hooks/`.
   - Symlinks `cch-edit.py` / `cch-write.py` / `ccm-get.py` into
     `~/.local/bin/` (collision-safe).
   - Registers PreToolUse entries in `~/.claude/settings.json`,
     appending to Bash so RTK fires first.
   - Writes routing snippet into `~/.claude/CLAUDE.md` between sentinel
     markers.

   Flags: `--no-instructions` skips CLAUDE.md, `--remove` undoes all
   three, `--check` is pre-flight only.

5. **Settings env (one-time tune):**
   ```jsonc
   // ~/.claude/settings.json
   {
     "env": {
       "CCH_CACHE_THRESHOLD": "2000"
     }
   }
   ```
   Takes effect on next Claude Code session start (memory `2336462276862`).

6. **Restart Claude Code** so PreToolUse entries activate.

### 5.2 Per-repo setup (one-time per repo)

For any repo where graph queries should work:

```
cd <repo>
crg build                    # ~3s for medium repo (~2.8s on cairn)
cairn-graph --location <known_symbol>   # smoke test
```

Repo's `.code-review-graph/graph.db` should be in `.gitignore`. Stale
graph is detected lazily (mtime check on query) and the affected file
is reparsed in 3-15 ms (measured 2026-05-02).

### 5.3 Verification

After all four installs + a Claude Code restart:

```
rtk gain                     # RTK live
cch-gain.py --dist           # CCH live, threshold = 2000
crg --version                # graph indexer live
cairn-graph --summary        # graph + cairn join live
python3 ~/Projects/cairn/cairn/query.py --stats
```

## 6. Configuration

### 6.1 Environment variables

| Var                    | Default | Purpose                                       |
| ---------------------- | ------- | --------------------------------------------- |
| `CCH_CACHE_THRESHOLD`  | `2000`  | Bytes above which Bash output is cached       |
| `CCH_CACHE_DIR`        | `~/.cache/cch` | Cache root                              |
| `CAIRN_HOME`           | `~/Projects/cairn` | Cairn install root                  |

### 6.2 Settings.json layout

```jsonc
{
  "env": { "CCH_CACHE_THRESHOLD": "2000" },
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",        "hooks": [ /* RTK first, CCH cache-wrap second */ ] },
      { "matcher": "Read",        "hooks": [ /* CCH multimodal allowlist */ ] },
      { "matcher": "Edit",        "hooks": [ /* CCH deny + cch-edit suggest */ ] },
      { "matcher": "Write",       "hooks": [ /* CCH deny + cch-write suggest */ ] },
      { "matcher": "NotebookEdit","hooks": [ /* CCH deny */ ] },
      { "matcher": "Grep",        "hooks": [ /* CCH deny + rg suggest */ ] },
      { "matcher": "Glob",        "hooks": [ /* CCH deny + fd/find suggest */ ] },
      { "matcher": "WebFetch",    "hooks": [ /* CCH deny + curl suggest */ ] }
    ],
    "UserPromptSubmit": [ /* cairn proactive context */ ],
    "Stop":             [ /* cairn memory capture */ ]
  }
}
```

`PostToolUse` is unused by design (DESIGN.md non-goal).

## 7. Hot-path lifecycle

Typical edit flow under the unified system:

```
1. cairn-graph --location foo
   → cairn/embeddings.py:412-468

2. sed -n '412,468p' cairn/embeddings.py
   → 57-line function body (inline, no cache)

3. cairn-graph --callers foo
   → 5 call sites (small, inline)

4. cch-edit.py cairn/embeddings.py 'old_snippet' 'new_snippet'
   → unified diff
   → impact line: callers:5 tests:2

5. cairn-graph --tests foo
   → tests/test_embeddings.py:140-167

6. pytest tests/test_embeddings.py::test_foo -x
```

Total context cost: ~150 lines of slices + 5 small graph rows + 1 diff,
versus a naive flow that would load 2-3 full files (~2,000+ lines).

## 8. Acceptance signals

| Metric                          | Target            | How measured                  |
| ------------------------------- | ----------------- | ----------------------------- |
| Correction rate                 | 1-2 / session     | Cairn correction memories tagged to claude-context-hooks (memory `2336462276822`) |
| Cache trip rate                 | 5-15% of Bash     | `cch-gain.py --dist`          |
| Orphan rate                     | < 30%             | `cch-gain.py --retrieval`     |
| Helper invocation share         | ≥ 95% of edits via cch-edit/cch-write | session JSONL audit |
| Graph query p95 latency         | < 50 ms           | per-call timing in `cairn-graph` wrapper |
| Per-task tokens vs naive baseline | ≥ 10× reduction on refactor tasks | `cch-gain.py --history` |

Acceptance is measured over real-session soak (≥ 1 week per memory
`2336462276822`), not in build sessions.

## 9. Coexistence rules

| Surface                   | Owner                                                         |
| ------------------------- | ------------------------------------------------------------- |
| `PreToolUse:Bash`         | RTK (rewrite, first) → CCH (cache wrapper, second)            |
| `PreToolUse:Read`         | CCH (block + multimodal allowlist)                            |
| `PreToolUse:Edit`         | CCH (block + redirect to `cch-edit`)                          |
| `PreToolUse:Write`        | CCH (block + redirect to `cch-write`)                         |
| `PreToolUse:NotebookEdit` | CCH (block + redirect to `cch-edit` / `jq`)                   |
| `PreToolUse:Grep`         | CCH (block + redirect to `rg`)                                |
| `PreToolUse:Glob`         | CCH (block + redirect to `fd` / `find`)                       |
| `PreToolUse:WebFetch`     | CCH (block + redirect to `curl`)                              |
| `UserPromptSubmit`        | cairn (proactive context)                                     |
| `Stop`                    | cairn (memory capture)                                        |
| `PostToolUse`             | unused (do not introduce without strong reason)               |

## 10. Non-goals

- Multi-user / multi-tenant. Personal-use focus.
- MCP-based tool exposure for graph queries. Bash transport only
  (memory `2336462276880`).
- Replacing cairn ingest with crg. Complementary; ingest is slow
  semantic distillation, crg is fast pure-AST (memory `2336462276883`).
- Per-tool caches outside Bash. CCH v1's mistake.
- Surfacing tool output through `permissionDecision: deny`. CCH v1's
  other mistake.
- Push-injection of graph data into PreToolUse. Pull only — pull
  surfaces are auditable in transcript (memory `2336462276866`).
- Per-tool cache for non-Bash paths. CCH v1's mistake.
- Two-strike Read or any speculative bypass. Hard deny only.

## 11. Open questions

| Question                                              | Status                                                              |
| ----------------------------------------------------- | ------------------------------------------------------------------- |
| Graph freshness cadence                               | Lazy-on-first-query (3-15 ms reparse, measured 2026-05-02). Watcher only if whole-repo summary queries become common. |
| Cache-wrap footer breadcrumb visibility               | OPEN. Memory `2336462276876`: footer can be stubbed inside `[CCM_CACHED]`. Fix: cache-wrap promotes recognised marker lines into stub header. |
| Symbol semantic search                                | OPEN. Embedding over `(symbol_name + docstring + leading_comment)` from crg AST. Closes "I want the function that handles X" without exact name. |
| In-band session-level cost signal                     | OPEN. No closed feedback loop for the model's own token spend. Stop-hook breadcrumb candidate. |
| Bash-death recovery                                   | OPEN. Memory `2336462276840`: cache-wrap import-failure leaves no internal recovery path. Graceful-degrade mode worth designing. |
| `cairn-graph --history SYMBOL` (git+cairn+graph join) | DEFERRED. No single primitive crosses git ↔ cairn ↔ graph today. |
| `cairn-graph --pack-callers SYMBOL` one-shot          | DEFERRED until refactor pain shows up in soak.                      |

## 12. Things not to reintroduce

From CCH v1 + first-pass v2 mistakes + lessons in this spec:

- Two-strike Read for "Edit-intent". Model learns the bypass; defeats
  Bash routing.
- Reason-gate via Read tool input. Read schema strips unknown fields
  before the hook sees them (`additionalProperties: false`).
- Per-tool caches for non-Bash paths.
- Surfacing successful results through `permissionDecision: deny`.
- Pushing graph data into PreToolUse injection. Pull only.
- Building a unified `cch-graph-edit` super-command. Composition via
  Bash is the value; a fat command hides the seams that make the system
  auditable.
- Pre-fetching all callers' source slices when `--callers` is queried.
  Trades startup latency for caching that often won't be used.
- Persisting a cache-key → symbol mapping. Symbol-aware `ccm-get` should
  re-resolve via the graph each call. Avoids cross-system schema
  coupling.

## 13. Empirical workload affinity

(From DESIGN.md `## Empirical workload affinity`, summarised.)

RTK and CCH compress along different axes and are complementary:

- **RTK** shines on command-volume sessions (git / grep / ls — many
  small commands, per-call shrinkers).
- **CCH** shines on inspection-volume sessions (large outputs from
  `python3 -c` dumps, log scans, DB queries — wrapper catches arbitrary
  large output, 10K-100K tokens per cached event).

cairn-graph extends the CCH regime: graph queries themselves are small
text (RTK regime), but they *direct* slice-reads that stay small by
construction (CCH regime, but with the slice predetermined rather than
discovered post-hoc). The graph turns "I had to cache because I read
too much" into "I read exactly what I needed".

## 14. References

- `docs/DESIGN.md` — v2 architectural rationale (read this for *why*).
- `docs/CLAUDE_MD_SNIPPET.md` — current routing-table text.
- Cairn memory IDs cited inline (e.g. `2336462276880`) — full context
  via `python3 ~/Projects/cairn/cairn/query.py --context <id>`.
