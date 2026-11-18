#!/usr/bin/env bash
# Run a private Home Assistant on this Mac (http://localhost:8124) with Automation Advisor,
# for connecting to your real Home Assistant read-only. Creates ./mac_advisor on first run.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
CFG="$ROOT/mac_advisor"

if [ ! -x .venv/bin/hass ]; then
  echo "Setting up Python environment (first run, a few minutes)..."
  command -v uv >/dev/null || { echo "Install uv first: brew install uv"; exit 1; }
  uv venv --python 3.14 .venv
  uv pip install --python .venv pytest-homeassistant-custom-component home-assistant-frontend
fi

if [ ! -f "$CFG/configuration.yaml" ]; then
  mkdir -p "$CFG/custom_components"
  ln -sfn "$ROOT/custom_components/automation_advisor" "$CFG/custom_components/automation_advisor"
  cat > "$CFG/configuration.yaml" <<'YAML'
homeassistant:
  name: Automation Advisor (Mac)
http:
  server_port: 8124
frontend:
config:
automation: !include automations.yaml
logger:
  default: warning
  logs:
    custom_components.automation_advisor: info
    haffmpeg: critical
    homeassistant.components.camera.img_util: critical
YAML
  echo "[]" > "$CFG/automations.yaml"
fi

# Home Assistant only adopts a changed HTTP port after it is confirmed in the UI and otherwise
# falls back to 8123. Pin the stored HTTP config to 8124 so a restart never moves the port.
if [ -f "$CFG/.storage/http" ]; then
  .venv/bin/python - "$CFG/.storage/http" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
data = d.get("data", {})
stable = data.get("stable") or {}
if stable.get("server_port") != 8124 or data.get("pending"):
    stable["server_port"] = 8124
    data["stable"], data["pending"] = stable, None
    json.dump(d, open(p, "w"), indent=2)
    print("Pinned the HTTP port to 8124")
PY
fi

echo "Starting on http://localhost:8124 (Ctrl-C to stop)"
exec .venv/bin/hass -c "$CFG"
