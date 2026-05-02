# claude-context-hooks

**State: v2 redesign — design doc complete, no code yet.**

## What this is

Lightweight Claude Code hook layer that minimises tool-output context cost
by **routing all data interaction through a single Bash data path**. Built-in
tools (Read/Grep/Glob/WebFetch) are blocked with narrow allowances; Bash
output is RTK-compressed and large residuals are cached for selective
querying. Coexists with [RTK](https://github.com/rtk-ai/rtk) and
[Cairn](https://github.com/jimovonz/cairn). Personal-use focus.

## Source of truth

[`docs/DESIGN.md`](docs/DESIGN.md) — purpose, single-data-path architecture,
why built-ins are blocked, routing policy, components, non-goals, open
questions. Read it before changing direction.

## Where we are right now

- v1 (per-tool caching, `ccm-get.py` retrieval, deny-channel surfacing)
  cleared from the working tree as staged deletions. **Not committed.**
- v2 design captured in `docs/DESIGN.md`, `README.md`, this file. **Not
  committed.**
- Public GitHub repo `jimovonz/claude-context-hooks` still shows v1 until
  the cutover is committed and pushed.
- Stash `pre-repurpose snapshot of intercept-bash.py changes` holds v1
  uncommitted edits. Recover with `git stash pop` if wanted, otherwise
  `git stash drop`.

## Architecture in one breath

Block built-in tools → all data goes through Bash → RTK compresses Bash →
wrapper caches residual large output → `ccm-get.py` retrieves selectively.
Cairn untouched.

Two distinct mechanisms, two different jobs:
- **Blocking** (Read/Grep/Glob/WebFetch) is the *routing* mechanism.
  Funnels everything to Bash via deny+suggest. Not about caching.
- **Wrapping** (Bash) is the *caching* mechanism. Our PreToolUse:Bash
  hook chains after RTK's: RTK rewrites first (`git status` → `rtk git
  status`), our hook rewrites second (adds cache wrapper). Bash runs the
  doubly-wrapped command. Output flows through normal `tool_result` —
  inline if small, stub-with-key if large. No deny-channel abuse.

RTK stays fully installed and we layer additively on its hook. We don't
remove or replace it.

## Immediate next steps

1. Resolve the open questions below (don't have to be perfect; pick a
   direction).
2. Commit the cutover (deletions + new `docs/DESIGN.md` + `README.md` +
   this `CLAUDE.md`).
3. Draft the routing-policy instruction snippet (text users add to their
   own CLAUDE.md).
4. Build `intercept-grep.py`, `intercept-glob.py`, `intercept-webfetch.py`
   first — these are unconditional blocks with redirect, simplest to
   implement.
5. Decide and build `intercept-read.py` allowlist policy.
6. Build the Bash cache wrapper and resolve the layering question with
   RTK's hook (chain or replace).
7. Carry over `lib/ccm_cache.py` and `ccm-get.py` from v1 (recover from
   git history at commit `6013673`).
8. `install.py` — symlink-based, registers hooks, verifies RTK present.
9. Push to GitHub.

## Open questions (resolve before coding)

- **Hook chaining protocol verification.** Design assumes Claude Code
  fires multiple PreToolUse:Bash hooks in settings.json-declared order
  with `updatedInput` propagating between them. Verify against Claude
  Code hook docs before implementation. Fallback if it doesn't work
  that way: replace RTK's hook with one of ours that calls `rtk rewrite`
  as a subprocess and adds cache wrapping in a single hook.
- **`intercept-read.py` allowlist.** Three candidates: multimodal-only
  (deny everything else, two-strike for Edit workflows); always-allow with
  `limit` injection (trust instruction); track Edit history per-session
  and allow Read on Edit-touched files.
- **Cache threshold.** v1 used 8KB. After RTK compression, may need
  raising to avoid caching small outputs that don't benefit.
- **CLAUDE.md instruction snippet wording.** Iterate against real use.

## Acceptance signal

Routing works when ~1–2 corrections per session is the steady-state rate.
Below that, enforcement is too lax; above, it's too aggressive.

## Dependencies (user prerequisite, not auto-installed)

- [RTK](https://github.com/rtk-ai/rtk) with its Claude Code Bash hook active
- [Cairn](https://github.com/jimovonz/cairn) with UserPromptSubmit + Stop hooks
- Python 3.10+
- Claude Code with `hookSpecificOutput.updatedInput` support

## Coexistence rules (do not violate)

| Surface              | Owner                                            |
| -------------------- | ------------------------------------------------ |
| `PreToolUse:Bash`    | RTK (rewrite, fires first) → this project (cache wrapper, fires second) |
| `PreToolUse:Read`    | this project (block + narrow allowlist)          |
| `PreToolUse:Grep`    | this project (block + redirect to `rg` via Bash) |
| `PreToolUse:Glob`    | this project (block + redirect to `fd` via Bash) |
| `PreToolUse:WebFetch`| this project (block + redirect to `curl` via Bash)|
| `UserPromptSubmit`   | Cairn (do not touch)                             |
| `Stop`               | Cairn (do not touch)                             |
| `PostToolUse`        | unused (do not introduce without strong reason)  |

## Don't reintroduce v1's mistakes

v1's caching was right; v1's *application* of caching was wrong:
- Cache was the primary mechanism on every tool path → v2 caches only
  Bash residuals (single path, after RTK compression).
- Output was surfaced through the deny channel → v2 surfaces through
  Bash's natural `tool_result` via wrapper-emitted stub.

If you find yourself wanting to add a per-tool cache for Read, Grep,
etc., or surface anything via `permissionDecision: deny` as a result
channel — stop. That's v1's path.
