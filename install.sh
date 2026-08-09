#!/usr/bin/env bash
# Install artifact-publisher on this Mac: CLI, launchd service, and global skill.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER="$(whoami)"
LABEL="com.$USER.artifact-server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/artifact-server.log"
CONFIG_DIR="$HOME/.artifacts"
CONFIG="$CONFIG_DIR/config.json"
SKILL_DIR="$HOME/.agents/skills/publish-artifact"

hostname="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
port=8787

chmod +x "$PROJECT_DIR/server.py" "$PROJECT_DIR/publish.py"

# 1. CLI — install into a directory that is actually on PATH
bin_dir=""
for d in "$HOME/.local/bin" "$HOME/bin" "/usr/local/bin"; do
  if printf '%s' "$PATH" | tr ':' '\n' | grep -qx "$d"; then bin_dir="$d"; break; fi
done
bin_dir="${bin_dir:-$HOME/.local/bin}"
mkdir -p "$bin_dir"
ln -sf "$PROJECT_DIR/publish.py" "$bin_dir/publish-artifact"
echo "installed CLI -> $bin_dir/publish-artifact"

# 2. Config + artifacts root
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<EOF
{
  "host": "0.0.0.0",
  "port": $port,
  "artifacts_root": "$HOME/artifacts",
  "auth": null,
  "public_base": "http://$hostname.local:$port"
}
EOF
  echo "created $CONFIG"
fi
mkdir -p "$HOME/artifacts"

# 3. Global skills
mkdir -p "$SKILL_DIR"
cp "$PROJECT_DIR/skill/publish-artifact/SKILL.md" "$SKILL_DIR/SKILL.md"
echo "installed skill -> $SKILL_DIR"
if [ -f "$PROJECT_DIR/skill/publish-prototype/SKILL.md" ]; then
  mkdir -p "$HOME/.agents/skills/publish-prototype"
  cp "$PROJECT_DIR/skill/publish-prototype/SKILL.md" "$HOME/.agents/skills/publish-prototype/SKILL.md"
  echo "installed skill -> $HOME/.agents/skills/publish-prototype"
fi

# 4. LaunchAgent
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$PROJECT_DIR/server.py</string>
    <string>--config</string>
    <string>$CONFIG</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
echo "installed LaunchAgent -> $PLIST"

# 5. Load it (bootstrap on modern macOS, fall back to load)
uid="$(id -u)"
if launchctl bootstrap "gui/$uid" "$PLIST" 2>/dev/null; then
  echo "bootstrapped $LABEL"
elif launchctl load -w "$PLIST" 2>/dev/null; then
  echo "loaded $LABEL (legacy)"
else
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    echo "$LABEL already running; reloading"
    launchctl bootout "gui/$uid" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$uid" "$PLIST"
  else
    echo "warning: could not start $LABEL"
  fi
fi

# 6. Health check
sleep 1
if curl -fsS "http://localhost:$port/healthz" >/dev/null 2>&1; then
  echo "server OK on http://localhost:$port (LAN: $(grep public_base "$CONFIG" | sed 's/.*: "\(.*\)".*/\1/'))"
else
  echo "server not responding yet; check $LOG"
fi
