#!/usr/bin/env bash
# Deliver locally and run as root. Restarts intake only; no live deploy in tests.
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/2387_common.sh"
phase=preflight
[[ $# -le 1 ]]
zip2387_source="${1:-$r4_tests/../code}"
zip2387_plan="$(python3 "$zip2387_lib" preflight "$zip2387_source")"
printf 'PLAN 2387:\n%s\n' "$zip2387_plan"
backup="$r4_root/root/md-zip-2387-$(date -u +%Y%m%dT%H%M%S)-$$"
phase=backup
mkdir -m 700 -- "$backup"
printf '%s\n' "$zip2387_plan" > "$backup/manifest.json"
cp -p -- "$r4_intake/intake_poller.py" "$backup/intake_poller.py"
python3 "$zip2387_lib" prepare "$backup"
for zip2387_name in rollback_2387.sh 2387_common.sh r4_common.sh deploy_2387_lib.py deploy_r4_lib.py; do
    cp -p -- "$r4_tests/$zip2387_name" "$backup/$zip2387_name"
done
python3 "$zip2387_lib" snapshot "$backup" before-stop
python3 "$zip2387_lib" backup-check "$backup"
python3 "$zip2387_lib" live-check "$backup"
touch "$backup/READY"
printf 'Backup: %s\nRollback: bash %q %q\n' "$backup" "$backup/rollback_2387.sh" "$backup"
phase=stop-intake
systemctl stop mycelium-intake
phase=drain
python3 "$zip2387_lib" drain "$backup"
phase=install
zip2387_attrs="$(python3 "$zip2387_lib" attrs "$backup")"
read -r zip2387_uid zip2387_gid zip2387_mode <<< "$zip2387_attrs"
r4_install "$backup/prepared-intake_poller.py" "$r4_intake/intake_poller.py" "$zip2387_uid" "$zip2387_gid" "$zip2387_mode"
python3 "$zip2387_lib" check "$backup"
phase=start-intake
zip2387_since="$(python3 "$zip2387_lib" now)"
systemctl start mycelium-intake
phase=health
python3 "$zip2387_lib" health "$zip2387_since"
python3 "$zip2387_lib" check "$backup"
phase=complete
printf 'PASS 2387 deploy; backup=%s\n' "$backup"
