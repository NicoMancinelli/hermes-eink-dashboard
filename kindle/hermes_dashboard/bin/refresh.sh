#!/bin/sh

ROOT="/mnt/us/extensions/hermes_dashboard"
LOG="/mnt/us/documents/hermes-dashboard.log"
printf '%s manual refresh\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG"
/bin/sh "$ROOT/bin/fetch.sh" once >> "$LOG" 2>&1
