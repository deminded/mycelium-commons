# file-safety-check

Static+AV gate for the Mycelium file-exchange circuit. Design source of
truth: `/opt/workspace/vault/Projects/Грибница/file-safety-design.md`
(6-phase design; this worker implements phases 2, 3, 5, 7).

**Canonical deployment: mycelium `/opt/mycelium/file-safety/` (user
`file-safety`), since 2026-07-07.** This copy on the main host is the dev
copy — edit and test here, then scp to mycelium.

## What it does

```
python3 file_safety_check.py <path>
# on mycelium:
runuser -u file-safety -- python3 /opt/mycelium/file-safety/file_safety_check.py <path>
```

1. **Static gate** (phase 2): real mime-type via `file` (magic bytes, not
   extension), whitelist, size limit (50MB), polyglot guard, and for zip
   archives a zip-bomb guard (stdlib central-directory read, no extraction):
   rejects on ratio >100x, >10000 files, >500MB uncompressed, >15 path
   levels, any nested archive.
2. **AV gate** (phase 3): `clamscan`; INFECTED → hard reject; clean is
   still only "quarantine". Missing binary/timeout → explicit `SKIPPED`,
   never a silent clean.
3. **Honest label** (phase 5): writes `<path>.safety.json`. `verdict` is
   *never* `"safe"` — only `"reject"` or `"quarantine"` (passed the gates
   we ran, still unverified semantically).
4. **Journal** (phase 7): appends one JSON line per check to `journal.log`
   next to the worker (override: env `FILE_SAFETY_JOURNAL`).

Exit code: `0` if verdict is `quarantine`, `1` if `reject`.

## Hardening (codex review 2026-07-07, closed on transfer to mycelium)

- **TOCTOU/symlink (#2):** target opened once with
  `O_NOFOLLOW|O_NONBLOCK`; size/type from `fstat(fd)`; `file`, `clamscan`,
  zip and hash readers all consume `/proc/self/fd/<fd>` (same open file
  description — a path swap mid-check can't redirect what gets scanned);
  label written only after re-checking the path still names the checked
  inode (`toctou_path_changed` reject otherwise); label opened with
  `O_NOFOLLOW`. Symlink target → reject; FIFO/device → reject
  (`not_regular_file`), FIFO open can't hang (O_NONBLOCK).
- **Polyglots (#3):** any non-zip that also opens as a zip (valid EOCD the
  way any unzip finds it) → reject `polyglot_zip_signature`; PNG/JPEG/GIF
  with bytes after the format terminator (IEND/EOI/trailer) → reject
  `trailing_data_after_*`. Ambiguity is not allowed through.

## What's NOT implemented yet

- **Phase 4 (LLM triage)**: needs the isolated uid sandbox — placeholder
  user `fs-triage` exists on mycelium, provisioning (one-shot tool-less
  reader, no secrets/network/memory) is the next tact. Stub returns
  `SKIPPED:no-isolation`.
- **Download wiring**: files shared in the Грибница supergroup are not yet
  auto-fetched into `incoming/` — credentials decision pending (dedicated
  download-bot token on mycelium, NOT the userbot session).

## Config

Whitelist, size limit, and zip quotas are constants at the top of
`file_safety_check.py` (`MIME_WHITELIST_*`, `MAX_FILE_SIZE_BYTES`,
`ZIP_MAX_*`) — edit there, not inline in the logic.

## Regression suite

12 cases, run on every change (see transfer validation 2026-07-07):
pdf→q, EICAR→r, normal.zip→q, nested.zip→r, oversize→r, symlink→r,
clean.png→q, polyglot.png→r, trailing.png→r, FIFO→r, clean.jpg→q, txt→q.

## What a "quarantine" verdict actually means

Passing the gates is NOT a safety guarantee. It says nothing about
prompt-injection or semantic content — see `cookbook-agent.md` in
`mycelium-commons`, section "Файлы = недоверенные данные", for the
processing discipline that actually protects an agent reading a
`quarantine`-labelled file.
