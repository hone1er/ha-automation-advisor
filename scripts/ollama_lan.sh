#!/usr/bin/env bash
# Run Ollama on this Mac so other devices on your home network (your Home Assistant Pi) can use
# it, only while you want it.
#
#   scripts/ollama_lan.sh start    start Ollama, reachable on the network (port 11434)
#   scripts/ollama_lan.sh stop     stop Ollama completely
#   scripts/ollama_lan.sh status   is it running, and who can reach it?
#
# Nothing starts at login: after a restart of the Mac, Ollama is off until you run "start".
# While it runs, anything on your Wi-Fi can use the model (Ollama has no password).
# macOS may ask once whether "ollama" may accept incoming connections: choose Allow.
set -euo pipefail

LABEL="local.ollama.lan"
DIR="$HOME/.ollama-lan"            # not in ~/Library/LaunchAgents, so it never starts at login
PLIST="$DIR/$LABEL.plist"
OLLAMA="$(command -v ollama || echo /opt/homebrew/bin/ollama)"
DOMAIN="gui/$(id -u)"

listening() { lsof -nP -iTCP:11434 -sTCP:LISTEN 2>/dev/null | tail -n +2; }

case "${1:-}" in
  start)
    # Homebrew's own service starts Ollama at every login, for this Mac only: switch it off.
    brew services stop ollama >/dev/null 2>&1 || true
    mkdir -p "$DIR" "$HOME/Library/Logs"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$OLLAMA</string><string>serve</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key><string>0.0.0.0:11434</string>
    <key>OLLAMA_FLASH_ATTENTION</key><string>1</string>
    <key>OLLAMA_KV_CACHE_TYPE</key><string>q8_0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/ollama-lan.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/ollama-lan.log</string>
</dict>
</plist>
PLIST
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "$DOMAIN" "$PLIST"
    for _ in $(seq 1 20); do listening >/dev/null && [ -n "$(listening)" ] && break; sleep 0.5; done
    echo "Ollama is running and reachable on your network:"
    listening
    echo
    echo "URL for Home Assistant: http://$(scutil --get LocalHostName).local:11434"
    echo "            or by IP:  http://$(ipconfig getifaddr en0 || ipconfig getifaddr en1):11434"
    echo "Stop it with: $0 stop"
    ;;
  stop)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    brew services stop ollama >/dev/null 2>&1 || true
    sleep 1
    if [ -n "$(listening)" ]; then
      echo "Something still listens on 11434:"; listening
    else
      echo "Ollama is stopped."
    fi
    ;;
  status)
    if [ -z "$(listening)" ]; then
      echo "Ollama is not running."
    elif listening | grep -q "127.0.0.1:11434"; then
      echo "Ollama is running for this Mac only (not reachable from the Pi)."
    else
      echo "Ollama is running and reachable on your network."
    fi
    ;;
  *)
    echo "usage: $0 start|stop|status" >&2
    exit 1
    ;;
esac
