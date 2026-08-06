#!/bin/sh
set -eu

PURGE="0"
[ "${1:-}" = "--purge" ] && PURGE="1"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

# Disable current and any pre-consolidation (hermes-kindle-dashboard) service.
for unit in hermes-eink-dashboard hermes-kindle-dashboard; do
  systemctl --user disable --now "$unit.service" >/dev/null 2>&1 || true
  rm -f "$CONFIG_HOME/systemd/user/$unit.service"
done
systemctl --user daemon-reload
rm -rf "$DATA_HOME/hermes-eink-dashboard" "$DATA_HOME/hermes-kindle-dashboard"
if [ "$PURGE" = "1" ]; then
  rm -rf "$CONFIG_HOME/hermes-eink-dashboard" "$CONFIG_HOME/hermes-kindle-dashboard"
  echo "Removed service, application, configuration, and token."
else
  echo "Removed service and application. Configuration/token preserved."
fi
