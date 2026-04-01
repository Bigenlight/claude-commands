#!/usr/bin/env bash
set -euo pipefail

# uninstall.sh — Remove Claude Code custom commands that were installed by install.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMANDS_SRC="$SCRIPT_DIR/commands"
CLAUDE_DIR="$HOME/.claude/commands"

if [ ! -d "$CLAUDE_DIR" ]; then
    echo "Nothing to uninstall: $CLAUDE_DIR does not exist."
    exit 0
fi

removed=0

for src_file in "$COMMANDS_SRC"/*.md; do
    [ -f "$src_file" ] || continue

    filename="$(basename "$src_file")"
    dest_file="$CLAUDE_DIR/$filename"

    if [ -e "$dest_file" ] || [ -L "$dest_file" ]; then
        rm -f "$dest_file"
        echo "Removed: $dest_file"
        removed=$((removed + 1))
    else
        echo "Not found (skip): $dest_file"
    fi
done

echo ""
echo "Done. Removed $removed command(s) from $CLAUDE_DIR"
