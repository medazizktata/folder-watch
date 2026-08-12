# folder-watch

Watch a folder until files finish writing, then run a pluggable **action**.

**Location:** `~/Desktop/folder-watch` (local git repo)  
**Global CLIs:** `~/.local/bin/{folder-watch,watch-copy,watch-unzip,watch-convx}` → `bin/`

## Install

```bash
cd ~/Desktop/folder-watch
.venv/bin/pip install -e .
```

Puts `folder-watch`, `watch-copy`, `watch-unzip` and `watch-convx` in
`.venv/bin/`, each pointing at the interpreter that owns `watchdog` — so they
run from any directory with no `PYTHONPATH` set. `python -m folder_watch` works
the same way.

The `bin/` wrappers still work without installing at all: they set `PYTHONPATH`
themselves and run the package out of the checkout. That's why the LaunchAgent
uses them — it works either way.

## How watching works

**Default: `--backend event`** (FSEvents via `watchdog`) — **copy** / **unzip**

1. FS event → **debounce** → **settle** (write-handles closed + size stable)
2. Skip files already finished at startup (**new-only**; use `--catch-up` to process old)
3. Run action; persist done-state (scheme B) so restarts don’t redo work

**convx** wraps `convx watch` (ConvX owns FSEvents).

## Actions

| Action | Behavior | Defaults |
|--------|----------|----------|
| `copy` | Atomic copy → `--to DIR` (retry on OSError); never deletes source; macOS notif. Mirrors subdirectories under `--recursive` | `--ext mp4 --min-bytes 1000000` |
| `unzip` | Extract `.zip`; delete zip only after **every** member is verified present. `--no-clobber` refuses an archive that would overwrite | `--ext zip --min-bytes 1` |
| `convx` | → `convx watch` (in-place) |
| `service` | LaunchAgent install / uninstall / status |

## Reliability / logging

```bash
# dry-run (no writes)
folder-watch copy ./Out --to ./Drive --once --dry-run

# log file + JSON lines
folder-watch --preset la-chaise-v1 --log ~/.local/state/folder-watch/la-chaise.log --json

# verbose settle/progress
folder-watch --preset la-chaise-v1 -v

# disable persistent done-state
folder-watch copy ./Out --to ./Drive --no-done-store

# copy retries (default 5)
folder-watch copy ./Out --to ./Drive --retries 8

# status of running watchers
folder-watch status
folder-watch status --preset la-chaise-v1

# drop done-state whose source is gone, and stale pidfiles (dry by default)
folder-watch prune
folder-watch prune --yes
```

## Files already in the folder at startup

A new watcher skips whatever is already sitting there (that's the point of
**new-only**), and records it as `baseline` — *assumed* handled. Nothing used to
verify that assumption, so a file that had never been processed looked exactly
like one that had, and was skipped forever without a word.

Startup now says so:

```
baseline ignored=2 encoding=0 pending=1 probed=2
baseline_pending count=1 names=['never-touched.zip'] hint=skipped but no output found
```

To ask the same question without starting anything — read-only, no state
written, no `lsof`, safe against a folder a watcher is running on right now:

```bash
folder-watch unzip ~/Inbox --mode named --pending
folder-watch --preset la-chaise-v1 --pending
```

Exits 1 if anything is pending. `--catch-up` then processes them, and clears
any stale `baseline` assumptions first — without that it would skip precisely
the files it was asked to pick up.

## When the folder goes away

**Watched folder deleted, renamed, or its volume unmounted** — FSEvents just goes
quiet, so the watcher polls for the directory every 5s rather than trusting the
silence. On loss it logs `src_lost`, tears down the observer and waits; when the
folder returns it logs `src_restored` and re-arms.

A restore always does a **catch-up** scan, even without `--catch-up`: anything
that landed while the directory was gone is work that hasn't been done. The
done-store makes that safe — files already handled are skipped, not redone.

**Destination missing** — `--to` is *not* created recursively. On an unmounted
drive `mkdir -p` happily rebuilds the whole path as ordinary local folders, so
files land on the boot disk, get reported `copied`, and get recorded as done —
silently never reaching the drive. Instead the copy fails with
`dest_unavailable` and is **not** marked done. `--create-dest` opts back into
building the full path.

## Retry queue

A failure the action judges *transient* — `dest_unavailable`, a `copy_error`
that outlived `atomic_copy`'s own backoff, an `OSError` mid-extract — is queued
rather than dropped. Nothing else would pick it up: the only thing that
re-queues a file is a new FSEvent on the **source**, and a destination coming
back produces none.

```
fail name=take.mp4 reason=dest_unavailable
retry_scheduled name=take.mp4 attempt=1 in_s=30
retry_waiting queued=1 reason=dest_away      # one probe, not one per file
retry name=take.mp4 attempt=1
copied name=take.mp4
```

Backoff runs 30 s → 60 s → 2 m → 5 m → 10 m → 30 m (capped), so a long outage
costs a handful of attempts an hour. Retries skip the settle loop entirely —
the file already settled once, and re-settling would pay another `lsof` per
attempt — so the recorded `(size, mtime)` is checked instead; if it moved, the
file was rewritten and goes back through the normal path.

The queue is persisted beside the done-store and survives a restart; baseline
seeding will not claim a file the queue owns. Terminal failures are never
queued: a corrupt archive does not become valid by waiting.

`--retry-hours` (default **72**) is when it stops and logs
`give_up reason=retry_exhausted`. 72 covers a weekend with the machine asleep —
timestamps are wall-clock, because monotonic clocks do not survive a restart,
so sleep counts against the budget. Giving up is **not** data loss: the source
is untouched, nothing was marked done, and `--pending` and `--catch-up` still
see the file.

Ignores partial/scratch names: `*.part`, `*.tmp`, `*.crdownload`, `.aria2`, etc.
Also skips **anything beginning with `.` or `~`** — that covers `._*`, `~$…` and
dotfiles, but note it excludes ordinary names like `~final cut.mp4` too.
Note `copy` only looks at `.mp4` of at least 1 MB unless you pass `--ext` /
`--min-bytes`; a file below the threshold is logged as `ignored`.

**Exit codes:** `--once` returns 1 if any file ended in `fail` or `error`, 0
otherwise (an empty run is not a failure), 2 for usage errors — including a
source directory that doesn't exist. So a cron or CI wrapper can actually tell.

**Log format:** plain-text fields escape control characters and are quoted when
they contain spaces, `=` or `"`, so `name="my clip.mp4"` rather than an
ambiguous bare value. Without that a crafted filename could emit a second,
well-formed line. `--json` was never affected.

**A stalled file is not abandoned.** If nothing changes for `STALL_TIMEOUT`
(300 s) the watcher asks `lsof` directly. If the writer has let go, the file is
complete and gets processed — logged `settled_late`, because releasing a handle
does not reliably emit an FSEvent and dropping it there lost finished work. If
the writer still holds it, it logs `stalled` once and keeps re-checking slowly
rather than forgetting it; only after 24 h of no progress does it `give_up`.

**Two watchers on one folder** log a `duplicate_watcher` warning and both act.
`--exclusive` refuses to start instead; it is off by default because under
launchd a refusing service flaps rather than running.

Done-state files: `~/.local/state/folder-watch/done-*.json`  
Pidfiles: `~/.local/state/folder-watch/pids/*.json`  
Key = action + src + dest/mode + size + mtime (scheme **B**).

## LaunchAgent

```bash
folder-watch service install --preset la-chaise-v1
folder-watch service status --preset la-chaise-v1
folder-watch service uninstall --preset la-chaise-v1
```

Keeps the preset running at login (`KeepAlive`). Logs default to `~/.local/state/folder-watch/<preset>.log`.

## Usage

```bash
folder-watch copy ./Output --to ./Drive/V2 --ext mp4
folder-watch unzip ~/Downloads/inbox
folder-watch convx ~/Inbox --format webp --ext png,jpg
folder-watch --preset la-chaise-v1

watch-copy ./Output ./Drive/V2
watch-unzip ~/Downloads/inbox
watch-convx ~/Inbox --format webp --ext png,jpg
```

Presets: `~/.config/folder-watch/presets.json`

## Local config (`.env`)

Machine-local paths and account identifiers stay out of source:

```bash
cp .env.example .env
```

Read from `$FOLDER_WATCH_ENV`, then `~/.config/folder-watch/.env`, then the
repo root — first one found wins, and a real environment variable overrides the
file. Optional: presets saved in `presets.json` need no `.env` at all. It only
populates the built-in defaults, which are omitted when unset.

## Tests

```bash
cd ~/Desktop/folder-watch
.venv/bin/python -m unittest discover -s tests -v
./bin/test-addons
```

For manual/interactive testing — a simulated encode that holds its write handle
open, test zips, and a state reset — see `~/Desktop/folder-watch-lab/`.

Lint:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff check . --fix
```

Config lives in `pyproject.toml`. `E402` is ignored under `tests/` — both
suites must put the repo on `sys.path` and import `tests/_isolate` (which
redirects state off `~/.local/state`) before `folder_watch` is imported.
