#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_COMMANDS_DIR="$HOME/.claude/commands"
PLUGIN_INSTALL_DIR="$HOME/.claude/plugins/marketplaces/claude-plugins-official/plugins/claude-commands"

echo "Installing claude-commands plugin..."

# Legacy: symlink individual commands to ~/.claude/commands/
mkdir -p "$CLAUDE_COMMANDS_DIR"
for md in "$REPO_DIR/commands/"*.md; do
  [ -f "$md" ] || continue
  ln -sf "$md" "$CLAUDE_COMMANDS_DIR/$(basename "$md")"
  echo "  Linked command: $(basename "$md")"
done

# New: Install as a Claude Code plugin
mkdir -p "$(dirname "$PLUGIN_INSTALL_DIR")"
if [ -L "$PLUGIN_INSTALL_DIR" ]; then
  rm "$PLUGIN_INSTALL_DIR"
fi
ln -sf "$REPO_DIR" "$PLUGIN_INSTALL_DIR"
echo "  Installed plugin: claude-commands -> $PLUGIN_INSTALL_DIR"

echo ""
echo "Installation complete!"
echo "Restart Claude Code for the 'orchestrate' skill to appear."
