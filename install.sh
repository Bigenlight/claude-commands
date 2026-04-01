#!/usr/bin/env bash
set -euo pipefail

# install.sh — Install Claude Code custom commands by linking (or copying) them
# into ~/.claude/commands/

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMANDS_SRC="$SCRIPT_DIR/commands"
CLAUDE_DIR="$HOME/.claude/commands"

# Detect OS / environment
detect_os() {
    case "$(uname -s)" in
        Darwin*)  echo "macos" ;;
        Linux*)   echo "linux" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *)        echo "unknown" ;;
    esac
}

OS="$(detect_os)"
echo "Detected OS: $OS"
echo "Source:       $COMMANDS_SRC"
echo "Destination:  $CLAUDE_DIR"
echo ""

# Create destination if it doesn't exist
if [ ! -d "$CLAUDE_DIR" ]; then
    echo "Creating $CLAUDE_DIR ..."
    mkdir -p "$CLAUDE_DIR"
fi

# Count installed
installed=0
skipped=0

for src_file in "$COMMANDS_SRC"/*.md; do
    [ -f "$src_file" ] || continue  # skip if no .md files

    filename="$(basename "$src_file")"
    dest_file="$CLAUDE_DIR/$filename"

    # Check if destination already exists
    if [ -e "$dest_file" ] || [ -L "$dest_file" ]; then
        echo "File already exists: $dest_file"
        read -rp "  Overwrite? [y/N] " answer
        case "$answer" in
            [yY]|[yY][eE][sS])
                rm -f "$dest_file"
                ;;
            *)
                echo "  Skipped: $filename"
                skipped=$((skipped + 1))
                continue
                ;;
        esac
    fi

    # Install: symlink on macOS/Linux, copy on Windows (symlinks need admin)
    if [ "$OS" = "windows" ]; then
        cp "$src_file" "$dest_file"
        echo "Copied:   $filename -> $dest_file"
    else
        ln -s "$src_file" "$dest_file"
        echo "Linked:   $filename -> $dest_file"
    fi

    installed=$((installed + 1))
done

echo ""
echo "Done. Installed: $installed, Skipped: $skipped"
