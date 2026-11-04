#!/usr/bin/env bash
# Runtime directories, offset, labels and journal are never chmod/chown/restored.
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/r4_common.sh"
phase=rollback-preflight
[[ $# -eq 1 ]]
backup="$1"
python3 "$r4_lib" validate-backup "$backup"
phase=rollback-stop-intake
systemctl stop mycelium-intake
phase=rollback-stop-worker
systemctl stop fs-triage-worker
phase=rollback-files
for r4_name in triage_daemon.py md_convert.py intake_poller.py; do
    r4_directory="$r4_triage"
    [[ "$r4_name" != intake_poller.py ]] || r4_directory="$r4_intake"
    r4_present="$(python3 "$r4_lib" existed "$backup" "$r4_name")"
    if [[ "$r4_present" == 1 ]]; then
        r4_attrs="$(python3 "$r4_lib" attrs "$backup" "$r4_name")"
        read -r r4_uid r4_gid r4_mode <<< "$r4_attrs"
        r4_install "$backup/$r4_name" "$r4_directory/$r4_name" "$r4_uid" "$r4_gid" "$r4_mode"
    else
        rm -f -- "$r4_directory/$r4_name"
    fi
done
phase=rollback-drop-in
rm -f -- "$r4_drop"
r4_present="$(python3 "$r4_lib" existed "$backup" drop)"
if [[ "$r4_present" == 1 ]]; then
    # cp -p to a fresh name, then replace; restore the prior drop-in exactly.
    r4_temporary="$(mktemp "${r4_drop}.r4.XXXXXXXX")"
    cp -p -- "$backup/prior-drop-in.conf" "$r4_temporary"
    mv -fT -- "$r4_temporary" "$r4_drop"
    r4_temporary=''
fi
phase=rollback-reload
systemctl daemon-reload
phase=rollback-start-worker
systemctl start fs-triage-worker
phase=rollback-start-intake
systemctl start mycelium-intake
phase=rollback-status
systemctl is-active mycelium-intake
systemctl is-active fs-triage-worker
printf 'PASS r4 rollback; backup=%s; runtime snapshots retained for reconciliation\n' "$backup"
