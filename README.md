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

Some actions don't run that loop at all: they **hand the folder to another
tool** that does its own watching. See [Wrapping another tool](#wrapping-another-tool).

## Actions

### Built in

| Action | Behavior | Defaults |
|--------|----------|----------|
| `copy` | Atomic copy → `--to DIR` (retry on OSError); never deletes source; macOS notif. Mirrors subdirectories under `--recursive` | `--ext mp4 --min-bytes 1000000` |
| `unzip` | Extract `.zip`; delete zip only after **every** member is verified present. `--no-clobber` refuses an archive that would overwrite | `--ext zip --min-bytes 1` |

`service` (LaunchAgent install / uninstall / status), `status`, `prune` and
`presets` are subcommands rather than actions.

## Wrapping another tool

Not every job is worth reimplementing. Plenty of tools already watch a folder
competently — converters, encoders, sync clients, uploaders — and the useful
thing folder-watch can add is not a second settle loop on top of theirs, but
the **operational shell around it**: one config format, one way to start it at
login, one place to see whether it is running, and a guarantee that it dies
when you stop it.

So there is a second kind of action: a **wrapper**. It converts nothing and
watches nothing. It translates folder-watch flags into the other tool's command
line, launches it, prints exactly what it ran, and then supervises the process.

```
folder-watch → /path/to/tool watch ~/Inbox -t webp --filter *.png --debounce 500
```

### What you get, and what you give up

A wrapper hands ownership of the watching to the wrapped tool, so most of this
README stops applying to it — the settle loop, the done-store and the retry
queue all belong to a pipeline folder-watch is no longer running:

| | built-in actions | wrapper actions |
|---|---|---|
| settle loop, `lsof`, `--stable` / `--settle` | yes | **no** — the wrapped tool decides when a file is ready |
| done-store, `--catch-up`, `baseline_pending` | yes | **no** |
| retry queue, `--retry-hours` | yes | **no** |
| `--pending` | yes | **no** |
| `--once` | yes | **no** — rejected when the wrapped command is continuous |

What you keep is the part that made you reach for a watcher in the first place:

* **Presets** — the same `--save-preset` / `--list-presets` / `presets forget`
  lifecycle, so a wrapped tool is configured the way everything else is.
* **`folder-watch status`** — a wrapper writes a pidfile like any other watch,
  so it shows up in the same table with the same log tail.
* **LaunchAgent** — `service install --preset NAME` works unchanged.
* **Lifecycle.** This is the one that actually earns its keep. A wrapped
  `watch` command runs forever, so anything that ends the wrapper without
  taking the child with it leaves an orphan watching the same folder — and
  those stack up silently over restarts. The wrapper turns `SIGTERM` into a
  clean unwind, terminates the child, escalates to `SIGKILL` if it will not
  go, and only then exits.

### The contract

A wrapper action is a small amount of code — an argv builder and a supervised
`exec` — and it commits to three things:

1. **Never bundle the tool.** A wrapped binary keeps its own repository,
   release cycle and issue tracker. `pip install folder-watch` must never
   drag it along, and for compiled tools it could not anyway.
2. **Fail loudly when it is missing.** Exit **127** with the binary name in
   the message, and accept an explicit path so a non-standard install still
   works, rather than silently doing nothing.
3. **Print the translated command line.** The flag mapping is the whole
   surface area of a wrapper; showing it means a mismatch is one glance away
   instead of a debugging session.

### Currently wrapped

**`convx`** → [ConvX](https://github.com/JSBtechnologies/convx), a third-party
"local-first universal file converter for CLI, Desktop, and AI agents". It is a
compiled binary, not a Python package, and it is not bundled, vendored or
installed by folder-watch. Install it separately, or point at it with
`--convx-bin /path/to/convx`; if it is absent the action exits **127** with
`error: convx not found`.

```bash
folder-watch convx ~/Inbox --format webp --ext png,jpg
```

`--ext` becomes ConvX's `--filter`, `--format` becomes `-t`, and
`--quality` / `--width` / `--fps` / `--max-size` / `--convx-preset` pass
through. ConvX converts in place, so `--to` is rejected rather than quietly
ignored, and `convx watch` is continuous, so `--once` is rejected too — use
`convx convert` directly for a one-shot run.

## Reliability / logging

```bash
# dry-run (no writes)
folder-watch copy ./Out --to ./Drive --once --dry-run

# log file + JSON lines
folder-watch --preset nightly-render --log ~/.local/state/folder-watch/nightly-render.log --json

# verbose settle/progress
folder-watch --preset nightly-render -v

# disable persistent done-state
folder-watch copy ./Out --to ./Drive --no-done-store

# copy retries (default 5)
folder-watch copy ./Out --to ./Drive --retries 8

# status of running watchers
folder-watch status
folder-watch status --preset nightly-render

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
folder-watch --preset nightly-render --pending
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
folder-watch service install --preset nightly-render
folder-watch service status --preset nightly-render
folder-watch service uninstall --preset nightly-render
```

Keeps the preset running at login (`KeepAlive`). Logs default to `~/.local/state/folder-watch/<preset>.log`.

## Usage

```bash
folder-watch copy ./Output --to ./Drive/V2 --ext mp4
folder-watch unzip ~/Downloads/inbox
folder-watch convx ~/Inbox --format webp --ext png,jpg
folder-watch --preset nightly-render

watch-copy ./Output ./Drive/V2
watch-unzip ~/Downloads/inbox
watch-convx ~/Inbox --format webp --ext png,jpg
```

Presets: `~/.config/folder-watch/presets.json`

```bash
folder-watch presets forget NAME          # dry — shows what it would remove
folder-watch presets forget NAME --yes
```

There are **no built-in presets** — they come only from these files. A preset
can be defined in more than one of them (a pre-rename `~/.config/watch-copy/`
path is still read), so `forget` names every file it found and removes the
preset from all of them; taking it out of one would leave the other still
being merged in and the command would look broken.

Removal is explicit and never a side effect of listing or running. It refuses
while a watcher is using that preset rather than pulling config out from under
it, and warns if a LaunchAgent is still installed for it. Exit 1 if the name is
unknown, 2 if it is in use.

`--save-preset NAME` **merges** into an existing preset rather than replacing
it, and says which it is doing:

```
note: preset 'job' already exists — modifying it, not creating it
  stable: 7 -> 9
updated preset 'job'
```

Only what you actually typed changes (plus anything you loaded with
`--preset`). Rebuilding the whole config from resolved values meant that
re-saving to change one field silently reset every other one to its default —
`ext` back to `mp4`, `min_bytes` back to `1000000`, `no_clobber` gone — while
reporting success. Creating a new preset still captures the full config, which
is what makes one useful.

`--list-presets` reports the health of each one rather than rendering a dead
preset identically to a live one:

```
nightly-render:  [source missing — nearest existing folder is ~/Desktop]
client-assets:
  ...
1 of 3 preset(s) need attention. Nothing was changed.
```

It reports and stops there. The tool cannot tell "this project is finished"
from "the drive is unplugged" — and its own runtime already assumes a missing
folder is temporary, which is what `src_lost` / `src_restored` are for. So it
never removes a preset for you.

It does distinguish the two cases where it honestly can, because they call for
opposite responses: a path whose nearest existing folder is an ordinary
directory was **deleted or renamed** and needs your attention, while one under
`/Volumes` or `~/Library/CloudStorage` is an **unmounted volume** and needs
nothing. Using a broken preset already fails loudly (`error reason=src_missing`,
exit 2); this only fixes the inventory view.

A preset can carry the safety flags too — `no_clobber`, `create_dest`,
`exclusive`, `no_done_store`, `retry_hours`. That matters because unattended
runs use `--preset`, so without it the options that most need to be on for an
unwatched watcher were the ones it could not reach. An explicit flag on the
command line still wins over the preset.

Nothing derives the preset schema from the argument parser, so a key the
loader doesn't read does nothing — and `--list-presets` used to echo it back
as though it were configuration. A misspelled `no_clober` looked exactly like
a working safety flag. Unrecognised keys now warn:

```
warn reason=unknown_preset_keys preset=demo keys=dry_run,no_clober note="ignored — not settable from a preset"
```

`PRESET_KEYS` and `NOT_PERSISTED` in `folder_watch/presets.py` are the two
lists, and a test fails if a flag is added to the parser without landing in
one of them.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
./bin/test-addons
```

```bash
make check    # lint + types + unit tests (~5s) — what the commit hook runs
make all      # adds the smoke suite and coverage
make dev      # install the dev dependencies
make install-hooks
```

`make check` runs on every commit via `hooks/pre-commit` (symlinked into
`.git/hooks` by `make install-hooks`, since that directory isn't versioned).
The smoke suite is deliberately outside the commit gate: it spawns real
watchers and does FSEvents timing, and a gate that is slow or flaky gets
bypassed with `--no-verify`, at which point it protects nothing.

GitHub Actions runs the same checks on **macOS runners** across Python 3.10
and 3.13 — a linux runner would be cheaper and would prove nothing about a
tool built on FSEvents, `lsof` and launchd. It also asserts `lsof` stays
reachable, since a watcher that cannot find it once treated every file as
permanently open and silently processed nothing.

Config lives in `pyproject.toml`. `E402` is ignored under `tests/` — every
suite must put the repo on `sys.path` and import `tests/_isolate` (which
redirects state off `~/.local/state`) before `folder_watch` is imported.

### Testing the watch loops

The loops are the hard part to test: they never return, their timeouts run to a
full day, and the event backend only wakes when the filesystem says so. So
`tests/_loopharness.py` replaces three things and nothing else —

* a **virtual clock** installed over the module's `time`, where sleeping
  advances the clock instead of blocking, and doubles as the tick callback a
  test uses to delete a directory, release a writer or skip past a timeout;
* a **stub observer** that captures the event handler instead of starting a
  watchdog thread, so events are a function call rather than a race;
* a **scripted `lsof`**, since whether a writer still holds a file is the one
  thing the settle logic cannot work out for itself.

Everything else is real: real files, real state, real queue. The result is that
`core/watch.py` — where all three audit rounds found their bugs — is covered at
96% by tests that finish in well under a tenth of a second and cannot flake on a
loaded machine. What is left uncovered there is error handling for filesystem
races that cannot be provoked without patching the standard library.

## Versioning and releases

`folder_watch/_version.py` is the single source of truth. `pyproject.toml`
reads it through hatch and `--version` prints it, so the wheel metadata, the
tag and the running binary cannot disagree — a test enforces that, and the
release workflow refuses a tag that does not match.

Versions are **semantic**, read from the point of view of someone who has a
watcher running unattended:

| | means |
|---|---|
| **major** | an existing invocation changes behaviour or stops working — a removed flag, a preset key that no longer means what it did, a different default |
| **minor** | new actions, flags or subcommands; existing setups keep working untouched |
| **patch** | fixes and internals only |

State on disk gets the same treatment as the CLI. The done-store, retry queue
and preset files are read by whatever version happens to be installed after an
upgrade, so a format change that an older version cannot read is a major bump.

### Cutting one

```bash
# record what changed under ## [Unreleased] in CHANGELOG.md first
make release V=0.5.0
git push origin main --follow-tags
```

`bin/release` refuses a dirty tree, a branch other than `main`, a
non-semantic version, a tag that already exists and an empty `[Unreleased]`
section. Then it runs `make all`, bumps the version, promotes `[Unreleased]`
to the new heading with today's date, commits and tags — and stops.

**Pushing the tag is the publish step**, and nothing before it leaves the
machine. `git tag -d v0.5.0 && git reset --soft HEAD~1` undoes the lot.

Once the tag lands, `.github/workflows/release.yml` re-runs the full gate at
that commit, checks the tag against `__version__`, builds the sdist and wheel,
and opens a GitHub Release using that version's changelog section as the notes.
A release is never published from a build that did not pass.

The first tag is a bootstrap — `bin/release` bumps, and 0.4.0 is already the
version in the tree:

```bash
git tag -a v0.4.0 -m "v0.4.0" && git push origin v0.4.0
```
