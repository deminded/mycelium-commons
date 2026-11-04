#!/usr/bin/env bash
# root, local files only. No packages/network/fstab/permission migration.
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/r4_common.sh"
phase=preflight
r4_source="${1:-$r4_tests/../code}"
[[ $# -le 1 ]]
# ALL validation below is read-only; a rejection cannot stop either unit.
r4_plan="$(python3 "$r4_lib" preflight "$r4_source")"
printf 'PLAN r4 (saved attributes; no permission migration):\n%s\n' "$r4_plan"
backup="$r4_root/root/md-convert-2350-$(date -u +%Y%m%dT%H%M%S)-$$"
phase=backup
mkdir -m 700 -- "$backup"
printf '%s\n' "$r4_plan" > "$backup/manifest.json"
python3 "$r4_lib" prepare-poller "$backup"
for r4_name in triage_daemon.py md_convert.py intake_poller.py; do
    r4_present="$(python3 "$r4_lib" existed "$backup" "$r4_name")"
    if [[ "$r4_present" == 1 ]]; then
        r4_directory="$r4_triage"
        [[ "$r4_name" != intake_poller.py ]] || r4_directory="$r4_intake"
        cp -p -- "$r4_directory/$r4_name" "$backup/$r4_name"
    fi
done
r4_present="$(python3 "$r4_lib" existed "$backup" drop)"
if [[ "$r4_present" == 1 ]]; then
    cp -p -- "$r4_drop" "$backup/prior-drop-in.conf"
fi
for r4_name in rollback_r4.sh r4_common.sh deploy_r4_lib.py check_deploy.py; do
    cp -p -- "$r4_tests/$r4_name" "$backup/$r4_name"
done
python3 "$r4_lib" snapshot "$backup" before-stop
# Marker is created only after all recovery files and state snapshots exist.
python3 "$r4_lib" backup-check "$backup"
touch "$backup/READY"
printf 'Backup: %s\nRollback: bash %q %q\n' "$backup" "$backup/rollback_r4.sh" "$backup"
phase=stop-intake
systemctl stop mycelium-intake
phase=drain
python3 "$r4_lib" drain "$backup"
phase=stop-worker
systemctl stop fs-triage-worker
# Capture again after the worker has exited, to expose a final publication/race.
python3 "$r4_lib" snapshot "$backup" stopped
python3 "$r4_lib" offset-check "$backup"
phase=install-code
for r4_name in triage_daemon.py md_convert.py intake_poller.py; do
    r4_attrs="$(python3 "$r4_lib" attrs "$backup" "$r4_name")"
    read -r r4_uid r4_gid r4_mode <<< "$r4_attrs"
    r4_directory="$r4_triage"
    [[ "$r4_name" != intake_poller.py ]] || r4_directory="$r4_intake"
    r4_new="$r4_source/$r4_name"
    [[ "$r4_name" != intake_poller.py ]] || r4_new="$backup/prepared-intake_poller.py"
    r4_install "$r4_new" "$r4_directory/$r4_name" "$r4_uid" "$r4_gid" "$r4_mode"
done
phase=create-directories
python3 "$r4_lib" dirs
phase=drop-in
# mkdir without -p only if absent: existing directory attributes never change.
if [[ ! -d "${r4_drop%/*}" ]]; then
    mkdir -m 755 -- "${r4_drop%/*}"
fi
python3 "$r4_lib" drop > "$backup/new-drop-in.conf"
r4_install "$backup/new-drop-in.conf" "$r4_drop" "$(id -u)" "$(id -g)" 644
phase=daemon-reload
systemctl daemon-reload
phase=start-worker
systemctl start fs-triage-worker
phase=verify-worker
systemctl is-active --quiet fs-triage-worker
r4_pid="$(systemctl show -p MainPID --value fs-triage-worker)"
[[ "$r4_pid" =~ ^[1-9][0-9]*$ ]]
nsenter -t "$r4_pid" -m findmnt --mountpoint "$r4_work" --output TARGET,FSTYPE,OPTIONS
python3 "$r4_tests/check_deploy.py" --mount-only
phase=start-intake
systemctl start mycelium-intake
phase=verify-intake
systemctl is-active --quiet mycelium-intake
python3 "$r4_lib" offset-check "$backup"
phase=check-deploy
python3 "$r4_tests/check_deploy.py" --backup "$backup"
phase=complete
printf 'PASS r4 deploy; backup=%s\n' "$backup"
