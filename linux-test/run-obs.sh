#!/usr/bin/env bash
# Starts OBS with its own config dir, so the test never touches ~/.config/obs-studio.
# WebSocket is pre-enabled on port 4455 with password "meettest": OBS 30 reads global.ini,
# OBS 31+ reads plugin_config/obs-websocket/config.json, so write both.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cfg="$here/obs-config/obs-studio"
mkdir -p "$cfg/plugin_config/obs-websocket"
[ -f "$cfg/plugin_config/obs-websocket/config.json" ] || cat > "$cfg/plugin_config/obs-websocket/config.json" <<'EOF'
{"alerts_enabled": false, "auth_required": true, "first_load": false,
 "server_enabled": true, "server_password": "meettest", "server_port": 4455}
EOF
[ -f "$cfg/global.ini" ] || cat > "$cfg/global.ini" <<'EOF'
[General]
FirstRun=true

[OBSWebSocket]
FirstLoad=false
ServerEnabled=true
ServerPort=4455
AlertsEnabled=false
AuthRequired=true
ServerPassword=meettest
EOF
XDG_CONFIG_HOME="$here/obs-config" exec obs --disable-shutdown-check --disable-missing-files-check "$@"
