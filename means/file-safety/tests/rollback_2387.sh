#!/usr/bin/env bash
# Restore code only. Never restore offset, journal or queues.
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/2387_common.sh"
phase=rollback-preflight
[[ $# -eq 1 ]]
backup="$1"
python3 "$zip2387_lib" validate-backup "$backup"
phase=rollback-stop-intake
systemctl stop mycelium-intake
phase=rollback-file
zip2387_attrs="$(python3 "$zip2387_lib" attrs "$backup")"
read -r zip2387_uid zip2387_gid zip2387_mode <<< "$zip2387_attrs"
r4_install "$backup/intake_poller.py" "$r4_intake/intake_poller.py" "$zip2387_uid" "$zip2387_gid" "$zip2387_mode"
python3 "$zip2387_lib" restored-check "$backup"
phase=rollback-start-intake
zip2387_since="$(python3 "$zip2387_lib" now)"
systemctl start mycelium-intake
phase=rollback-health
python3 "$zip2387_lib" health "$zip2387_since"
phase=complete
printf 'PASS 2387 rollback; backup=%s\n' "$backup"
