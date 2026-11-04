#!/usr/bin/env bash
# Offline, current uid only. --check-deploy reads permissions in the given directory.
set -euo pipefail
task_tests_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -eq 2 && $1 == --check-deploy ]]; then
    exec python3 "$task_tests_dir/check_deploy.py" "$2"
elif [[ $# -ne 0 ]]; then
    echo "usage: $0 [--check-deploy <directory>]" >&2
    exit 2
fi
task_smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/md-convert-smoke.XXXXXXXX")"
trap 'rm -rf -- "$task_smoke_dir"' EXIT
python3 "$task_tests_dir/md_convert_cases.py" --fixtures "$task_smoke_dir/fixtures"
for kind in pdf docx html; do
    echo "=== $kind ==="
    python3 "$task_tests_dir/../code/md_convert.py" \
        "$task_smoke_dir/fixtures/sample.$kind" "$kind" "$task_smoke_dir/$kind"
    if [[ -f "$task_smoke_dir/$kind/sample.md.zip" ]]; then
        python3 -m zipfile -l "$task_smoke_dir/$kind/sample.md.zip"
    fi
done
