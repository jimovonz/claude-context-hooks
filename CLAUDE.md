# claude-context-hooks

**State: v2.0.0 shipped 2026-05-02.**
Tagged `v2.0.0` on commit `e192fa6`, pushed to
[`jimovonz/claude-context-hooks`](https://github.com/jimovonz/claude-context-hooks).
Installed locally and operational.

## What this is

Lightweight Claude Code hook layer that minimises tool-output context cost
by **routing all data interaction through a single Bash data path**. Built-in
tools (`Read`, `Grep`, `Glob`, `WebFetch`, `Edit`, `Write`, `NotebookEdit`)
are blocked at the hook layer; `Read` of multimodal extensions is the only
allowed built-in path because Bash has no equivalent. Bash output is
RTK-compressed and large residuals cached for selective slice retrieval
via `ccm-get.py`. Edits and writes go through `cch-edit` and `cch-write`
helpers (Bash-routed) so the read-before-edit guard never fires.

Coexists with [RTK](https://github.com/rtk-ai/rtk) and
[Cairn](https://github.com/jimovonz/cairn). Personal-use focus.

## Source of truth

[`docs/DESIGN.md`](docs/DESIGN.md) — purpose, single-data-path architecture,
why built-ins are blocked (including the read-before-edit token tax that
forces writes to Bash), routing policy, components, non-goals, open
questions. Read it before changing direction.

## Where we are right now

- v2.0.0 tagged at `e192fa6`, pushed to GitHub `main`.
- All 101 tests pass.
- Installed locally: 14 symlinks in `~/.claude/hooks/`, 3 helper
  symlinks in `~/.local/bin/` (`cch-edit.py`, `cch-write.py`,
  `ccm-get.py`), 8 PreToolUse entries in `~/.claude/settings.json`.
- Live smoke test 2026-05-02 confirmed: RTK rewrite + cache stub +
  `ccm-get.py` slice retrieval + bare helper invocation + deny+redirect
  on Read/WebFetch/Write all work. Edit deny is shadowed by the harness
  read-before-edit guard but net effect (Edit unusable, must use
  `cch-edit.py`) is identical.
- RTK installed locally (v0.38.0, `~/.local/bin/rtk`); RTK's PreToolUse:Bash
  hook ordered before our cache wrapper in `~/.claude/settings.json`.
- Stash `pre-repurpose snapshot of intercept-bash.py changes` (`stash@{0}`)
  still present — pre-v2 snapshot of `intercept-bash.py`. Safe to drop
  with `git stash drop stash@{0}` once you've confirmed v2 is solid.

## Architecture in one breath

Block built-in tools → all data goes through Bash → RTK compresses Bash →
wrapper caches residual large output → `ccm-get.py` retrieves selectively.
Edits and writes routed through `cch-edit` / `cch-write` helpers so the
read-before-edit guard never fires. Cairn untouched.

Two distinct mechanisms, two different jobs:
- **Blocking** (Read/Grep/Glob/WebFetch/Edit/Write/NotebookEdit) is the
  *routing* mechanism. Funnels everything to Bash via deny+suggest.
  Multimodal Read is the only structurally-irreplaceable built-in.
- **Wrapping** (Bash) is the *caching* mechanism. Our PreToolUse:Bash
  hook chains after RTK's: RTK rewrites first (`cat foo.py` → `rtk cat
  foo.py`), our hook rewrites second (adds cache wrapper). Bash runs the
  doubly-wrapped command. Output flows through normal `tool_result` —
  inline if small, stub-with-key if large. No deny-channel abuse.

RTK stays fully installed and we layer additively on its hook. We don't
remove or replace it.

## Immediate next steps

1. Soak the design over real sessions — measure correction rate (target
   ~1-2/session) and iterate the CLAUDE.md routing snippet wording.
2. Optionally raise the cache threshold above 8KB once we see how often
   small post-RTK Bash outputs trip it.
3. Re-run `rtk discover --since 7` after a week of v2 use to confirm
   coverage rose well above the 2.9% pre-install baseline.
4. Sanity-check README still describes the v2 shape (helpers, PATH
   exposure via `~/.local/bin`, read-before-edit rationale).

## Open questions

- **Cache threshold (initial tune set to 2KB, soaking).** v1 used 8KB.
  Empirical `cch-gain.py --dist` over an early session showed RTK
  shrinks most output below 8KB so the wrapper barely tripped (1/92
  events). Set `CCH_CACHE_THRESHOLD=2000` in `~/.claude/settings.json`
  env block — should catch ~10% of commands while staying well above
  the ~550-byte break-even floor (visible-cost only). Watch
  `cch-gain.py --retrieval` for orphan rate over the soak week; bump
  back up if orphans >30%.
- **CLAUDE.md instruction snippet wording.** Iterate against real use.
  The current snippet covers helpers (`cch-edit`, `cch-write`) and the
  unconditional block on Edit/Write/NotebookEdit; correction rate from
  real sessions will tell us whether the helper syntax needs more
  prominence or worked-example flow.

(Resolved during build: hook chaining protocol — empirically works for
RTK + cch on Bash; intercept-read.py allowlist — multimodal-only with
writes via Bash helpers, no two-strike or reason-gate needed; helpers
on PATH — installer now symlinks `cch-edit.py` / `cch-write.py` /
`ccm-get.py` into `~/.local/bin/` collision-safely so bare
invocation works; gain reporting — `cch-gain.py` ships with
`--dist` for size histogram + threshold trial and `--retrieval`
for per-cache orphan/slice analysis; threshold tuning — settings.json
env block is the canonical override path.)

## Acceptance signal

Routing works when ~1–2 corrections per session is the steady-state rate.
Below that, enforcement is too lax; above, it's too aggressive. Cold-start
sessions naturally see more before the CLAUDE.md routing snippet
internalises.

## Dependencies (user prerequisite, not auto-installed)

- [RTK](https://github.com/rtk-ai/rtk) with its Claude Code Bash hook active
- [Cairn](https://github.com/jimovonz/cairn) with UserPromptSubmit + Stop hooks
- Python 3.10+
- Claude Code with `hookSpecificOutput.updatedInput` support

## Coexistence rules (do not violate)

| Surface                       | Owner                                                                   |
| ----------------------------- | ----------------------------------------------------------------------- |
| `PreToolUse:Bash`             | RTK (rewrite, fires first) → this project (cache wrapper, fires second) |
| `PreToolUse:Read`             | this project (block + multimodal allowlist)                             |
| `PreToolUse:Grep`             | this project (block + redirect to `rg` via Bash)                        |
| `PreToolUse:Glob`             | this project (block + redirect to `fd` via Bash)                        |
| `PreToolUse:WebFetch`         | this project (block + redirect to `curl` via Bash)                      |
| `PreToolUse:Edit`             | this project (block + redirect to `cch-edit` via Bash)                  |
| `PreToolUse:Write`            | this project (block + redirect to `cch-write` via Bash)                 |
| `PreToolUse:NotebookEdit`     | this project (block + redirect to `cch-edit` / `jq` via Bash)           |
| `UserPromptSubmit`            | Cairn (do not touch)                                                    |
| `Stop`                        | Cairn (do not touch)                                                    |
| `PostToolUse`                 | unused (do not introduce without strong reason)                         |

## Don't reintroduce v1's mistakes

v1's caching was right; v1's *application* of caching was wrong:
- Cache was the primary mechanism on every tool path → v2 caches only
  Bash residuals (single path, after RTK compression).
- Output was surfaced through the deny channel → v2 surfaces through
  Bash's natural `tool_result` via wrapper-emitted stub.

If you find yourself wanting to add a per-tool cache for Read, Grep,
etc., or surface anything via `permissionDecision: deny` as a result
channel — stop. That's v1's path.

Don't reintroduce v2's first-pass mistakes either:
- Two-strike Read for "Edit-intent" — leaks: model learns double-tap
  always works, defeats Bash routing. Replaced by the `cch-edit`
  helper which removes the read-before-edit coupling entirely.
- Reason-gate via Read tool input — structurally impossible because
  Read's tool schema has `additionalProperties: false`; constrained
  generation strips unknown fields before the hook ever sees them.
