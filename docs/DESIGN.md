# Design: claude-context-hooks (v2)

## Purpose

Lightweight Claude Code hook layer that minimises tool-output context cost
by routing all data interaction through Bash, where compression and caching
can be applied uniformly. Coexists with — does not replace —
[RTK](https://github.com/rtk-ai/rtk) and [Cairn](https://github.com/jimovonz/cairn).

Personal-use focus.

## Background

The previous iteration of this repo cached every tool's large output behind
keys retrievable via `ccm-get.py`, surfacing results through PreToolUse's
deny-with-reason channel. That had two distinct problems:

1. The deny channel was abused as a successful-result channel (UX noise,
   poor composition with Cairn's hooks).
2. Caching was applied as a per-tool concern across Bash, Read, Grep,
   Glob, and WebFetch — five intercept paths, five cache code paths,
   five sets of edge cases.

This redesign keeps the cache (it's the right answer for residual large
outputs) but fixes both problems by **routing all data interaction through
a single Bash data path**. Built-in tools are actively blocked with one
narrow exception (multimodal Read); everything else — including writes —
is rewritten to Bash via instruction. The cache becomes a wrapper on Bash
output, not a per-tool concern.

## Architecture

A single data path, three layers of treatment:

```
   ┌──────────────────────────────────────────────────────────┐
   │ Built-in tools                                           │
   │ Read / Grep / Glob / WebFetch / Edit / Write / Notebook  │
   └────────────────────────────┬─────────────────────────────┘
                                │
                                │ blocked by PreToolUse hooks
                                │ (only exception: Read of
                                │  multimodal extensions)
                                │
                                ▼   suggest bash equivalent
   ┌──────────────────────────────────────────────────────────┐
   │ Bash                                                     │
   │ inspection: cat / head / sed / rg / fd / curl            │
   │ writes:     cch-edit / cch-write                         │
   └────────────────────────────┬─────────────────────────────┘
                                │
                                ▼   PreToolUse: rtk rewrite + cache wrapper
   ┌──────────────────────────────────────────────────────────┐
   │ rtk-compressed command output                            │
   └────────────────────────────┬─────────────────────────────┘
                                │
                  ┌─────────────┴──────────────┐
                  ▼                            ▼
               inline                       cache + stub
              (small)                      (large residual)
                                                 │
                                                 ▼
                                ccm-get.py --grep / --head / --tail
```

**Layer 1 — Compression at source.** RTK rewrites Bash commands to
compressed equivalents (`git status` → `rtk git status`, `cat foo.py`
→ `rtk cat foo.py`). The bulk of outputs become small enough to inline
cleanly.

**Layer 2 — Cache for residuals.** A wrapper around the RTK-rewritten
command checks output size after compression. If above threshold, write
to a content-addressable cache and emit a stub on stdout. The bash tool's
`tool_result` is either inline (small) or stub-with-key (large). The
model retrieves selectively via `ccm-get.py` filters (`--grep`, `--head`,
`--tail`, `--lines`).

The wrapper is **fail-soft**: it reports exit 0 to the harness for every
inner-command run, carrying the real exit code in-band instead (an
`[exit N]` line on inline output, the stub's `exit:` field on cached
output). This exists because the Claude Code harness cancels every other
tool call in a parallel batch when any one reports a non-zero exit — and
inspection commands return non-zero as a matter of course (`grep`
no-match, `ls` of a missing path, `diff` finding differences). Splitting
the exit-code channel — success to the harness, real code in-band — means
benign failures never cancel sibling calls, so parallel Bash batches are
safe. Shell-internal semantics (`&&`, `set -e`) already resolved inside
the inner `bash -c` before the wrapper reports, so only the harness's
cascade decision changes. `CCH_PROPAGATE_EXIT=1` restores raw
propagation; wrapper-usage errors (bad argv) always propagate. Cached
content also carries a check digit so a garbled-in-transit stub is
detectable (`ccm-get.py --check`), and retrieval recomputes the BLAKE2s
key to catch on-disk corruption.

**Layer 3 — Memory.** Cairn surfaces past corrections at session start
via its own UserPromptSubmit retrieval. Passive cross-session
reinforcement, untouched by this project.

(The diagram shows the seven blocked data tools. A separate
`PreToolUse:Agent` hook redirects Explore sub-agents whose prompt is a
code-structure query to `cairn-graph` rather than spawning an agent with
un-routed tool access; all other agents pass through.)

## Mechanisms

Two distinct mechanisms doing two different jobs. Don't conflate them.

**Blocking** is the *routing* mechanism. PreToolUse hooks on Read, Grep,
Glob, WebFetch, Edit, Write, and NotebookEdit return
`permissionDecision: deny` with a `permissionDecisionReason` that names
the bash equivalent. The model retries via Bash. This isn't about
caching — it exists purely to funnel all data through one path so we
only need one mechanism downstream. Multimodal Read (image/PDF/notebook
extensions) is the single allowed built-in path because Bash has no
substitute that produces image content blocks for the multimodal model.

**Wrapping** is the *caching* mechanism. The Bash command flows through
RTK's PreToolUse hook (compression via `updatedInput` rewrite) and then
through our chained PreToolUse hook (further `updatedInput` rewrite to
add a cache wrapper around RTK's already-rewritten command). The Bash
tool then runs the doubly-wrapped command. The cache wrapper measures
output size, writes to cache and emits a stub if above threshold,
otherwise emits output unchanged. The bash tool's `tool_result` is the
wrapper's stdout — flows through the **normal channel**, not through a
deny channel.

These mechanisms are independent. Built-ins could be blocked without
caching; Bash could be cached without blocking built-ins. We do both
because together they ensure all data interaction is routed through one
compress-and-cache pipeline.

## Why blocking built-in tools (rather than letting them through)

Built-in tools (Read, Grep, Glob, WebFetch, Edit, Write, NotebookEdit)
execute inside Claude Code with output shape fixed by the Claude Code
source. There is no clean mechanism to compress or cache their output:
PreToolUse `updatedInput` modifies parameters but not output, and
PostToolUse output replacement is unverified. Routing everything through
Bash means one mechanism handles all output uniformly.

A second cost hides on the write side. Built-in `Edit`, `Write`, and
`NotebookEdit` enforce a **read-before-edit guard** — Claude Code
refuses to edit any path that has not had a successful built-in `Read`
in the same session. The Read tool returns the full file content as a
text block in conversation history, uncompressed and bypassing the
cache wrapper. So every Edit on a non-trivial file pays a token tax
equal to the full file size, exactly defeating the cache wrapper for
the files being edited.

Bash writes (`sed -i`, `tee`, heredoc, `>`) do not trigger this guard.
Routing edits through Bash via the `cch-edit` and `cch-write` helpers
preserves the cache wrapper on every file operation.

The single structural exception is **multimodal Read**: `Read foo.png`
returns image content blocks the multimodal model can interpret;
`cat foo.png` returns binary garbage. There is no Bash invocation that
injects an image into the conversation as something the model can see.
Multimodal extensions are the only allowed built-in Read path.

## Routing policy

| Workflow                            | Path                                    | Treatment                                  |
| ----------------------------------- | --------------------------------------- | ------------------------------------------ |
| Inspection / search of text files   | Bash (`cat`, `head`, `tail`, `rg`, `fd`)| RTK compress → inline-or-cache             |
| Multimodal (.png/.pdf/.ipynb/.jpg)  | Read built-in (allowed)                 | Pass through (typically small)             |
| Web content                         | Bash (`curl`, `wget`)                   | RTK compress → inline-or-cache             |
| File listing                        | Bash (`fd`, `find`, `ls`)               | RTK compress → inline-or-cache             |
| Literal-string edit                 | Bash (`cch-edit`)                       | No read-before-edit cost; atomic; diff out |
| File write / overwrite              | Bash (`cch-write`)                      | No read-before-edit cost; atomic           |
| Notebook edit                       | Bash (`cch-edit` on .ipynb / `jq` / `nbformat`) | Notebook is JSON; literal match works for source edits |

Acceptable deviation rate: ~1–2 corrections per session.

## Example flow

A worked example for a Bash command whose post-compression output is
large enough to cache.

```
1. Model issues:
     Bash(command="rg some-pattern src/")

2. PreToolUse:Bash chain fires:
     a. RTK's hook (first):
          calls `rtk rewrite "rg some-pattern src/"`
          returns updatedInput.command = "rtk grep some-pattern src/"
     b. Our cch hook (second):
          sees the rewritten command
          returns updatedInput.command = "<cache-wrapper> rtk grep some-pattern src/"

3. Bash tool executes the doubly-wrapped command.
     - Wrapper invokes rtk-compressed grep as subprocess
     - Captures stdout, measures size
     - Output is 50KB after RTK compression — above threshold
     - Wrapper writes content to cache (key = abc123def0)
     - Wrapper emits stub on its own stdout

4. Bash tool_result (normal channel, no deny):
     [CCM_CACHED]
     ~tokens: 12k
     lines: 487
     [/CCM_CACHED]
     Retrieve: ccm-get.py abc123def0 [--grep PATTERN] [--head N] [--tail N] [--lines A-B]

5. Model decides what slice it actually needs and issues:
     Bash(command="ccm-get.py abc123def0 --grep 'error|warn'")

6. PreToolUse:Bash chain fires again:
     - RTK's hook: no rewrite for ccm-get.py (passes through)
     - Our cch hook: rewrites to "<cache-wrapper> ccm-get.py abc123def0 --grep 'error|warn'"

7. Bash tool executes:
     - Wrapper invokes ccm-get.py — outputs filtered subset (small)
     - Wrapper sees output below threshold — emits unchanged
     - Inline result returns to model
```

The same wrapper machinery handles step 7's small output uniformly with
step 3's large output — it's one code path, output size decides which
branch fires.

## Components

### CLAUDE.md instruction snippet

Concise routing-policy text, suitable for inclusion in a project's
`CLAUDE.md` or `~/.claude/CLAUDE.md`. States the policy, names the
preferred bash commands and Bash-routed write helpers.

### PreToolUse enforcement hooks

| Hook                          | Default | Allowlist                                      |
| ----------------------------- | ------- | ---------------------------------------------- |
| `intercept-read.py`           | block   | multimodal extensions only (.png/.jpg/.jpeg/.gif/.webp/.bmp/.pdf/.ipynb/.svg) |
| `intercept-grep.py`           | block   | none — always redirect to `rg` via Bash        |
| `intercept-glob.py`           | block   | none — always redirect to `fd`/`find` via Bash |
| `intercept-webfetch.py`       | block   | none — always redirect to `curl` via Bash      |
| `intercept-edit.py`           | block   | none — always redirect to `cch-edit` via Bash  |
| `intercept-write.py`          | block   | none — always redirect to `cch-write` via Bash |
| `intercept-notebookedit.py`   | block   | none — always redirect to `cch-edit` / `jq` / `nbformat` via Bash |
| `intercept-agent.py`          | pass    | redirect Explore agents whose prompt is a code-structure query to `cairn-graph`; all other agents pass |

Each hook is small (~30–50 lines), reads tool input from stdin, decides
allow/deny, emits valid `hookSpecificOutput`. Deny responses include a
`permissionDecisionReason` naming the bash equivalent.

### Bash cache wrapper

Wraps the RTK-rewritten command. Captures output, measures size, writes
to cache and emits stub if above threshold; otherwise emits output
unchanged. Surfaced through Bash's natural `tool_result` channel — no
deny-channel abuse. Fail-soft: reports exit 0 to the harness and carries
the real exit code in-band (`[exit N]` / stub `exit:`) so a benign
non-zero exit never cancels sibling tool calls in a parallel batch (see
Layer 2). `CCH_PROPAGATE_EXIT=1` restores raw propagation.

Layering: our PreToolUse:Bash hook is registered **after** RTK's in
`~/.claude/settings.json`. RTK's hook fires first and produces an
`updatedInput` with the rtk-rewritten command. Our hook fires second,
sees the rewritten command, further rewrites it to wrap in cache logic.
Bash runs the doubly-wrapped command. RTK stays fully installed and
keeps its full feature set; we are additive, not replacing.

### `cch-edit.py`

Bash-routed alternative to built-in `Edit`. Replicates the safety
contract:

- Exact literal match (no regex)
- Errors if `old_string` not found
- Errors if `old_string` occurs more than once unless `--all` is passed
- Atomic write (temp file + rename)
- Prints unified diff on success

```
cch-edit.py /path 'old' 'new'                              # single-line
cch-edit.py /path --old-file /tmp/o --new-file /tmp/n      # multi-line
cch-edit.py /path 'old' 'new' --all                        # all occurrences
```

Runs via Bash so the read-before-edit guard never fires and the cache
wrapper is preserved on the edit target.

### `cch-write.py`

Bash-routed alternative to built-in `Write`. Reads content from stdin
(no shell-escaping required), writes atomically (temp + rename),
creates parent directories on demand.

```
echo 'content' | cch-write.py /path
cat source.txt | cch-write.py /path
cch-write.py /path << 'EOF'
multi-line content with $vars and `backticks` not expanded
EOF
```

### `cch-batch.py`

Runs many independent commands concurrently in a single tool call.
Commands are read from stdin (one per line; blanks and `#`-comments
skipped), run in parallel through `cache-wrap.py`, and emitted as one
delimited block per command in input order. Because it is a single tool
call there are no sibling calls for the harness to cancel — cascade-immune
by construction — and it gives real wall-clock parallelism for deliberate
fan-out. `--jobs N` caps concurrency (default 8); `--no-cache-wrap` runs
each command via plain `bash -c`.

### `lib/ccm_cache.py`

Content-addressable cache. BLAKE2s hashing, zstd compression with gzip
fallback, deduplication. Stubs carry a check digit and retrieval verifies
the content key, so corruption (in transit or on disk) is detectable.

### `ccm-get.py`

Filtered retrieval of cached output. Carry over from v1.

```
ccm-get.py <key> --grep PATTERN
ccm-get.py <key> --head N
ccm-get.py <key> --tail N
ccm-get.py <key> --lines A-B
ccm-get.py <key> --symbol NAME    # function body via graph.db
ccm-get.py --check                # verify a pasted stub is intact (stdin)
```

User-facing refusals (bad/stale key, reason-gate) print to stderr but
exit 0, so they do not trigger the parallel-batch cascade.

### `install.py`

Symlink-based installer. Registers hooks in `~/.claude/settings.json`.

Pre-install verification:
- `rtk` binary is on PATH (else: link to RTK install instructions)
- RTK's Claude Code hook is present and registered (else: tell user to
  run `rtk init -g --auto-patch`)
- Our PreToolUse:Bash hook entry is registered **after** RTK's in
  settings.json (chain ordering matters — RTK rewrites first, we wrap
  second)

Does not auto-install RTK (user prerequisite). Does not modify or remove
RTK's hook entries; only appends our own.

## Out of scope (explicit non-goals)

- Per-built-in caching — built-ins are blocked, not cached
- PostToolUse output replacement — unverified protocol territory; not
  needed since built-ins are blocked
- Backwards compatibility with v1 cache key formats
- Support for non-Claude-Code agents
- Automatic RTK installation or version management

## Open questions

1. **Hook chaining protocol verification.** The design assumes Claude
   Code's PreToolUse fires multiple registered hooks in
   settings.json-declared order, with the `updatedInput` from each hook
   propagating to the next. Empirically this works for the RTK + cch
   chain on Bash; revisit if Anthropic changes the contract. Fallback
   if it ever breaks: replace RTK's hook with one of ours that calls
   `rtk rewrite` as a subprocess and adds cache wrapping in a single
   hook.
2. **Cache threshold.** Default 8KB (`CCH_CACHE_THRESHOLD` env var).
   v1 used 8KB. After RTK compression, typical Bash output is small
   enough that 8KB rarely trips — early empirical `cch-gain.py --dist`
   data showed only ~1% of commands exceeded 8KB. Lower threshold
   (e.g. 2KB) catches more genuine wins, with the floor set by
   round-trip overhead (~138 visible tokens per ccm-get retrieval ≈
   ~550 bytes break-even). Override via `~/.claude/settings.json`
   `env` block:
   ```json
   { "env": { "CCH_CACHE_THRESHOLD": "2000" } }
   ```
   Tune empirically with `cch-gain.py --dist` (size distribution +
   threshold trial) and `cch-gain.py --retrieval` (orphan rate +
   slice ratios). High orphan rate (caches the model never reads) is
   the signal that the threshold has been pushed too low.
3. **CLAUDE.md instruction wording.** Needs iteration on real usage to
   maximise compliance. The current snippet covers the maximally-clean
   shape (multimodal Read + Bash-routed everything else); the
   correction rate from real sessions will tell us whether the helper
   syntax (`cch-edit`/`cch-write`) needs more prominence.

(Resolved during build: `intercept-read.py` allowlist mechanism — going
multimodal-only and routing edits through `cch-edit` removes the
read-before-edit coupling entirely, so no two-strike or reason-gate
machinery is needed. The Read tool was always going to drag the full
file into context to satisfy the read-before-edit guard, defeating
caching on the very files being edited; replacing built-in Edit/Write
with Bash helpers breaks that coupling.)

## Dependencies (user prerequisites, not auto-installed)

- Python 3.10+
- Claude Code with `hookSpecificOutput.updatedInput` support
- RTK installed and its Claude Code Bash hook active
- Cairn installed and operational
- Optional: `zstandard` (better cache compression; falls back to gzip),
  `tiktoken` (accurate token counting; falls back to char estimate)

## Coexistence rules

| Surface                       | Owner                                                                   |
| ----------------------------- | ----------------------------------------------------------------------- |
| `PreToolUse:Bash`             | RTK (rewrite, fires first) → this project (cache wrapper, fires second) |
| `PreToolUse:Read`             | this project (block + multimodal allowlist)                             |
| `PreToolUse:Grep`             | this project (block + redirect to Bash)                                 |
| `PreToolUse:Glob`             | this project (block + redirect to Bash)                                 |
| `PreToolUse:WebFetch`         | this project (block + redirect to Bash)                                 |
| `PreToolUse:Edit`             | this project (block + redirect to `cch-edit`)                           |
| `PreToolUse:Write`            | this project (block + redirect to `cch-write`)                          |
| `PreToolUse:NotebookEdit`     | this project (block + redirect to `cch-edit` / `jq`)                    |
| `PreToolUse:Agent`            | this project (pass; redirect code-structure Explore agents to `cairn-graph`) |
| `UserPromptSubmit`            | Cairn (do not touch)                                                    |
| `Stop`                        | Cairn (do not touch)                                                    |
| `PostToolUse`                 | unused (do not introduce)                                               |

### Empirical workload affinity

RTK and CCH compress along different axes and become complementary
under different session shapes:

- **RTK shines on command-volume sessions.** Lots of small `git` /
  `grep` / `ls` invocations where each command has a dedicated
  per-command shrinker (`rtk git status`, `rtk grep`, etc.). Saves
  60-90% per command, low absolute volume per call. Wrapper rarely
  trips because outputs are individually small after RTK.

- **CCH shines on inspection-volume sessions.** Large `python3 -c`
  data dumps, ad-hoc DB queries, log scans — outputs RTK has no
  shrinker for. CCH wraps with a stub-and-retrieve pattern so the
  model only pays for the slice it actually wants. Saves order-of-
  10K-100K tokens per cached event, low call frequency.

A representative early observation (2026-05-02): one session was 99%
CCH-driven savings (~37K tokens stubbed) and 1% RTK-driven (~300
tokens compressed) because the work was inspection-heavy. Another
session in the same period was the opposite. The architecture's
purpose is to capture both regimes without forcing the user to choose.

## Migration from v1

The previous `claude-context-hooks` cached on every tool path. v2 keeps
the cache library and retrieval tool but applies them only to Bash
output, and additionally blocks built-in writes (`Edit`, `Write`,
`NotebookEdit`) in favour of Bash-routed `cch-edit` / `cch-write`
helpers. Migration is a clean cut: uninstall v1, install RTK, install v2.

There is no compatibility shim for v1 cache keys.
