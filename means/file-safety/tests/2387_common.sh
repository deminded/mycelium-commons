# Reuse r4 atomic install/cleanup and offline path redirection.
source "$(dirname -- "${BASH_SOURCE[0]}")/r4_common.sh"
zip2387_lib="$r4_tests/deploy_2387_lib.py"
r4_error() {
    local rc="$1"
    trap - ERR INT TERM
    set +e
    r4_cleanup
    echo "FAIL 2387 phase=$phase exit=$rc backup=$backup" >&2
    printf 'mycelium-intake: ' >&2
    systemctl is-active mycelium-intake >&2
    if [[ -f "$backup/READY" ]]; then
        echo 'Backup READY; reconcile runtime snapshots if stop interrupted a task.' >&2
        printf 'Rollback command: bash %q %q\n' "$backup/rollback_2387.sh" "$backup" >&2
    else
        echo 'Backup NOT READY; intake has not been stopped by this recipe.' >&2
    fi
    exit "$rc"
}
