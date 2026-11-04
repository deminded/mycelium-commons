# Sourced by deploy_r4.sh / rollback_r4.sh. All server paths can be redirected
# into a temporary fixture with MD_R4_TEST_ROOT (offline command-model tests).
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
r4_tests="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
r4_lib="$r4_tests/deploy_r4_lib.py"
r4_root="${MD_R4_TEST_ROOT:-}"
r4_triage="$r4_root/opt/mycelium/fs-triage"
r4_intake="$r4_root/opt/mycelium/file-safety"
r4_drop="$r4_root/etc/systemd/system/fs-triage-worker.service.d/md-convert-work.conf"
r4_work="$r4_triage/work"
phase=initial
backup=not-created
r4_temporary=''
r4_cleanup() {
    if [[ -n "$r4_temporary" ]]; then
        rm -f -- "$r4_temporary"
    fi
}
r4_error() {
    local rc="$1"
    trap - ERR INT TERM
    set +e
    r4_cleanup
    echo "FAIL r4 phase=$phase exit=$rc backup=$backup" >&2
    printf "mycelium-intake: " >&2
    systemctl is-active mycelium-intake >&2
    printf "fs-triage-worker: " >&2
    systemctl is-active fs-triage-worker >&2
    if [[ -f "$backup/READY" ]]; then
        echo 'Backup READY (code/drop-in only; runtime snapshots are for reconciliation).' >&2
    else
        echo 'Backup NOT READY; services have not been stopped by this recipe.' >&2
    fi
    printf 'Rollback command: bash %q %q\n' "$backup/rollback_r4.sh" "$backup" >&2
    echo 'Inspect saved queue/offset/journal snapshots before resuming intake; never reset offset.' >&2
    exit "$rc"
}
trap 'r4_error $?' ERR
trap 'r4_error 130' INT
trap 'r4_error 143' TERM
trap r4_cleanup EXIT
r4_install() {
    local source="$1" target="$2" owner="$3" group="$4" mode="$5"
    r4_temporary="$(mktemp "${target}.r4.XXXXXXXX")"
    install -o "$owner" -g "$group" -m "$mode" -- "$source" "$r4_temporary"
    mv -fT -- "$r4_temporary" "$target"
    r4_temporary=''
}
