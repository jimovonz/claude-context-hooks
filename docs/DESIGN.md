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
a single Bash data path**. Built-in tools are actively blocked with narrow
allowances; everything else is rewritten to Bash via instruction. The cache
becomes a wrapper on Bash output, not a per-tool concern.

## Architecture

A single data path, three layers of treatment:

```
   ┌────────────────────────────────────┐
   │ Built-in tools                     │
   │ Read / Grep / Glob / WebFetch      │
   └──────────────┬─────────────────────┘
                  │
                  │ blocked by PreToolUse hooks
                  │ (narrow allowlist for Read:
                  │  multimodal + edit-before-Read)
                  │
                  ▼   suggest bash equivalent
   ┌────────────────────────────────────┐
   │ Bash                               │
   └──────────────┬─────────────────────┘
                  │
                  ▼   PreToolUse: rtk rewrite + cache wrapper
   ┌────────────────────────────────────┐
   │ rtk-compressed command output      │
   └──────────────┬─────────────────────┘
                  │
       ┌──────────┴───────────┐
       ▼                      ▼
    inline                  cache + stub
   (small)                 (large residual)
                                │
                                ▼
                        ccm-get.py --grep / --head / --tail
```

**Layer 1 — Compression at source.** RTK rewrites Bash commands to
compressed equivalents (`git status` → `rtk git status`). The bulk of
outputs become small enough to inline cleanly.

**Layer 2 — Cache for residuals.** A wrapper around the RTK-rewritten
command checks output size after compression. If above threshold, write
to a content-addressable cache and emit a stub on stdout. The bash tool's
`tool_result` is either inline (small) or stub-with-key (large). The
model retrieves selectively via `ccm-get.py` filters (`--grep`, `--head`,
`--tail`, `--lines`).

**Layer 3 — Memory.** Cairn surfaces past corrections at session start
via its own UserPromptSubmit retrieval. Passive cross-session
reinforcement, untouched by this project.

## Mechanisms

Two distinct mechanisms doing two different jobs. Don't conflate them.

**Blocking** is the *routing* mechanism. PreToolUse hooks on Read, Grep,
Glob, and WebFetch return `permissionDecision: deny` with a
`permissionDecisionReason` that names the bash equivalent. The model
retries via Bash. This isn't about caching — it exists purely to funnel
all data through one path so we only need one mechanism downstream.

**Wrapping** is the *caching* mechanism. The Bash command flows through
RTK's PreToolUse hook (compression via `updatedInput` rewrite) and then
through our chained PreToolUse hook (further `updatedInput` rewrite to
add a cache wrapper around RTK's already-rewritten command). The Bash
tool then runs the doubly-wrapped command. The cache wrapper measures
output size, writes to cache and emits a stub if above threshold,
otherwise emits output unchanged. The bash tool's `tool_result` is the
wrapper's stdout — flows through the **normal channel**, not through a
deny channel. From the LLM's perspective the Bash command always
"succeeded".

These mechanisms are independent. Built-ins could be blocked without
caching; Bash could be cached without blocking built-ins. We do both
because together they ensure all data interaction is routed through one
compress-and-cache pipeline.

## Why blocking built-in tools (rather than letting them through)

Built-in tools (Read, Grep, Glob, WebFetch) execute inside Claude Code
with output shape fixed by the Claude Code source. There is no clean
mechanism to compress or cache their output: PreToolUse `updatedInput`
modifies parameters but not output, and PostToolUse output replacement is
unverified. Routing everything through Bash means one mechanism handles
all output uniformly.

The two genuine cases for built-in Read are preserved by narrow
allowance:

- **Multimodal content** — `Read foo.png` returns image content blocks;
  `cat foo.png` returns binary garbage. Bash has no equivalent.
- **Read↔Edit coupling** — Claude Code's Edit tool requires a prior Read
  of the same file. Edit-intent reads must use Read built-in.

Grep, Glob, and WebFetch have full bash equivalents (`rg`, `fd`/`find`,
`curl`) and are blocked unconditionally.

## Routing policy

| Workflow                            | Path                                    | Treatment                                  |
| ----------------------------------- | --------------------------------------- | ------------------------------------------ |
| Inspection / search of text files   | Bash (`cat`, `head`, `tail`, `rg`, `fd`)| RTK compress → inline-or-cache             |
| Multimodal (.png/.pdf/.ipynb/.jpg)  | Read built-in (allowed)                 | Pass through (typically small)             |
| Read-before-Edit                    | Read built-in (allowed)                 | Pass through                               |
| Web content                         | Bash (`curl`, `wget`)                   | RTK compress → inline-or-cache             |
| File listing                        | Bash (`fd`, `find`, `ls`)               | RTK compress → inline-or-cache             |

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
preferred bash commands.

### PreToolUse enforcement hooks

| Hook                     | Default | Allowlist                                       |
| ------------------------ | ------- | ----------------------------------------------- |
| `intercept-read.py`      | block   | multimodal extension OR edit-intent (TBD)       |
| `intercept-grep.py`      | block   | none — always redirect to `rg` via Bash         |
| `intercept-glob.py`      | block   | none — always redirect to `fd`/`find` via Bash  |
| `intercept-webfetch.py`  | block   | none — always redirect to `curl` via Bash       |

Each hook is small (~30–50 lines), reads tool input from stdin, decides
allow/deny, emits valid `hookSpecificOutput`. Deny responses include a
`permissionDecisionReason` naming the bash equivalent.

### Bash cache wrapper

Wraps the RTK-rewritten command. Captures output, measures size, writes
to cache and emits stub if above threshold; otherwise emits output
unchanged. Surfaced through Bash's natural `tool_result` channel — no
deny-channel abuse.

Layering: our PreToolUse:Bash hook is registered **after** RTK's in
`~/.claude/settings.json`. RTK's hook fires first and produces an
`updatedInput` with the rtk-rewritten command. Our hook fires second,
sees the rewritten command, further rewrites it to wrap in cache logic.
Bash runs the doubly-wrapped command. RTK stays fully installed and
keeps its full feature set; we are additive, not replacing.

### `lib/ccm_cache.py`

Content-addressable cache. Carry over from v1 with no functional changes.
BLAKE2s hashing, zstd compression with gzip fallback, deduplication.

### `ccm-get.py`

Filtered retrieval of cached output. Carry over from v1.

```
ccm-get.py <key> --grep PATTERN
ccm-get.py <key> --head N
ccm-get.py <key> --tail N
ccm-get.py <key> --lines A-B
```

### `install.py`

Symlink-based installer. Registers hooks in `~/.claude/settings.json`.

Pre-install verification:
- `rtk` binary is on PATH (else: link to RTK install instructions)
- RTK's Claude Code hook (`~/.claude/hooks/rtk-rewrite.sh`) is present
  and registered (else: tell user to run `rtk init -g`)
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
   propagating to the next. This needs verification against Claude Code
   hook docs before implementation. If chaining behaves differently
   (e.g. only first hook runs, or `updatedInput` doesn't propagate),
   fallback is to replace RTK's hook with one of ours that calls `rtk
   rewrite` as a subprocess and adds cache wrapping in a single hook —
   more code, no chain-ordering uncertainty, RTK loses no functionality
   because tracking happens inside the `rtk` binary not the hook.
2. **`intercept-read.py` allowlist mechanism.** Detecting "edit-intent"
   reliably is hard. Candidate approaches:
   - Allow only multimodal extensions; treat all other Read as deny;
     accept that the model has to Read again after the deny when it
     intends to Edit (two-strike pattern).
   - Always-allow Read with default `limit` injected when unspecified;
     trust the CLAUDE.md instruction to nudge Bash for inspection.
   - Track recently-Edited files in session state; allow Read of
     anything Edit-touched.
3. **Cache threshold.** v1 used 8KB for Bash. After RTK compression,
   typical Bash output is small enough that 8KB may be too low —
   threshold may need raising to avoid caching things that don't need
   it.
4. **CLAUDE.md instruction wording.** Needs iteration on real usage to
   maximise compliance.

## Dependencies (user prerequisites, not auto-installed)

- Python 3.10+
- Claude Code with `hookSpecificOutput.updatedInput` support
- RTK installed and its Claude Code Bash hook active
- Cairn installed and operational
- Optional: `zstandard` (better cache compression; falls back to gzip),
  `tiktoken` (accurate token counting; falls back to char estimate)

## Coexistence rules

| Surface              | Owner                                            |
| -------------------- | ------------------------------------------------ |
| `PreToolUse:Bash`    | RTK (rewrite, fires first) → this project (cache wrapper, fires second) |
| `PreToolUse:Read`    | this project (block + narrow allowlist)          |
| `PreToolUse:Grep`    | this project (block + redirect to Bash)          |
| `PreToolUse:Glob`    | this project (block + redirect to Bash)          |
| `PreToolUse:WebFetch`| this project (block + redirect to Bash)          |
| `UserPromptSubmit`   | Cairn (do not touch)                             |
| `Stop`               | Cairn (do not touch)                             |
| `PostToolUse`        | unused (do not introduce)                        |

## Migration from v1

The previous `claude-context-hooks` cached on every tool path. v2 keeps
the cache library and retrieval tool but applies them only to Bash
output. Migration is a clean cut: uninstall v1, install RTK, install v2.

There is no compatibility shim for v1 cache keys.
