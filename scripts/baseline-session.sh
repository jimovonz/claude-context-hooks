#!/usr/bin/env bash
# Launch a Claude Code session with every optimisation layer disabled, so a
# prompt can be re-tested against baseline behaviour. Restores everything on
# exit (including Ctrl-C / kill).
#
# Disables:
#   - CCH PreToolUse + cache-wrap hooks (settings.json swap + CCH_DISABLE=1)
#   - Cairn UserPromptSubmit + Stop hooks (settings.json swap)
#   - cairn-graph CLI (PATH filter removes ~/.local/bin)
#   - RTK rewrite hook + binary (settings.json swap + PATH filter)
#   - ~/.claude/CLAUDE.md routing snippet (moved aside)
#   - ~/.claude/RTK.md (moved aside)
#   - ~/.claude/rules/memory-system.md (moved aside)
#   - Project CLAUDE.md (cd to a fresh empty tmp dir unless --here)
#
# Usage:
#   ./baseline-session.sh           # launches claude in a fresh tmp dir
#   ./baseline-session.sh --here    # launches in $PWD (project CLAUDE.md still loads)

set -euo pipefail

HERE_MODE=0
[[ "${1:-}" == "--here" ]] && HERE_MODE=1

STAMP="$(date +%Y%m%d-%H%M%S)-$$"
BACKUP_DIR="/tmp/baseline-session-${STAMP}"
mkdir -p "$BACKUP_DIR"

CLAUDE_DIR="$HOME/.claude"
declare -a STASHED=()

stash() {
    local src="$1"
    if [[ -e "$src" ]]; then
        local dest="$BACKUP_DIR/$(basename "$src")"
        mv "$src" "$dest"
        STASHED+=("$src::$dest")
        echo "  stashed $src -> $dest"
    fi
}

restore_all() {
    echo
    echo "Restoring stashed files..."
    for entry in "${STASHED[@]}"; do
        local src="${entry%%::*}"
        local dest="${entry##*::}"
        if [[ -e "$dest" ]]; then
            mv "$dest" "$src"
            echo "  restored $src"
        fi
    done
    rmdir "$BACKUP_DIR" 2>/dev/null || true
    echo "Done."
}

trap restore_all EXIT INT TERM

echo "=== baseline-session: disabling all optimisations ==="
echo "Backup dir: $BACKUP_DIR"
echo

stash "$CLAUDE_DIR/settings.json"
stash "$CLAUDE_DIR/CLAUDE.md"
stash "$CLAUDE_DIR/RTK.md"
stash "$CLAUDE_DIR/rules/memory-system.md"

echo '{}' > "$CLAUDE_DIR/settings.json"
echo "  wrote empty $CLAUDE_DIR/settings.json"

# Filter ~/.local/bin out of PATH (kills cairn-graph, rtk, ccm-get, cch-*)
FILTERED_PATH="$(echo "$PATH" | tr ':' '\n' | grep -vF "$HOME/.local/bin" | paste -sd:)"
export PATH="$FILTERED_PATH"
export CCH_DISABLE=1

echo
echo "PATH (filtered): $PATH"
echo "CCH_DISABLE=$CCH_DISABLE"
echo

if [[ $HERE_MODE -eq 0 ]]; then
    WORK_DIR="$(mktemp -d -t baseline-session-XXXXXX)"
    echo "cd $WORK_DIR (fresh dir, no project CLAUDE.md)"
    cd "$WORK_DIR"
fi

echo
echo "=== launching claude ==="
echo "Quit with /quit to trigger restore."
echo
claude || true
