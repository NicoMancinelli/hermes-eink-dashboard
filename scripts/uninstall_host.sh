#!/bin/sh
set -eu

PURGE="0"
[ "${1:-}" = "--purge" ] && PURGE="1"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

# Both the current and the pre-rename names are removed so upgrades never
# leave a stale unit or an orphaned virtualenv behind.
for name in hermes-eink-dashboard hermes-kindle-dashboard; do
  systemctl --user disable --now "$name.service" >/dev/null 2>&1 || true
  rm -f "$CONFIG_HOME/systemd/user/$name.service"
  rm -rf "${DATA_HOME:?}/$name"
done
systemctl --user daemon-reload
if [ "$PURGE" = "1" ]; then
  rm -rf "${CONFIG_HOME:?}/hermes-eink-dashboard"
  rm -rf "${CONFIG_HOME:?}/hermes-kindle-dashboard"
  rm -rf "${CONFIG_HOME:?}/hermes-kindle-dashboard.migrated"
  echo "Removed service, application, configuration, and token."
else
  echo "Removed service and application. Configuration/token preserved."
fi
