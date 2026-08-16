# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); see
[Versioning and releases](README.md#versioning-and-releases) for what a bump
means and how one is cut.

Entries are written for someone deciding whether to upgrade. A fix says what
went wrong and what it cost, because for a tool that runs unattended, "what was
silently not happening" is the part that matters.

0.1.0 is the first release. The work in it predates any versioning — the tool
was built and run unattended for weeks before there was a number to put on it —
which is why the first entry covers so much ground.

## [Unreleased]

## [0.2.1] - 2026-08-16

### Fixed

- **`service install` could not point a LaunchAgent at an installed copy.** It
  only understood the checkout layout — a clone with a `.venv` beside it — so
  run from a `pip install`ed venv it fell through to the base interpreter and
  wrote a plist naming a Python with no `folder_watch` on it. The service then
  crash-looped on "No module named folder_watch" with KeepAlive restarting it,
  which is a failure that never surfaces at install time: the plist is valid
  and launchd accepts it. The venv is now detected by its `pyvenv.cfg`, before
  any `resolve()` — a venv's `bin/python` is a symlink to the base interpreter,
  so resolving first walks straight out of the venv. An installed copy also
  gets **no `PYTHONPATH`**: naming a checkout there would put the checkout
  ahead of site-packages and quietly hand the service the working tree back.

## [0.2.0] - 2026-08-16

### Changed

- **`--once` now honours `--stable` and `--settle` set in a preset**, not only
  on the command line. It kept its fast one-shot defaults over both, so a
  preset that raised them because that encoder needs longer to finish was
  silently dropped for single-pass runs — and those two settings are exactly
  what decides whether a file is finished being written. A `--once` run using
  such a preset will now take as long as the preset asks for.

### Fixed

- **A partial extraction read as a finished one.** `output_exists` answered
  "is there anything in the target directory at all", so one member of two
  hundred landing counted as complete — and `--pending` and `baseline_pending`
  are built on that answer. The run that most needs reporting, an interrupted
  or failed extract, was the one getting a false all-clear. It now asks the
  archive what it should have produced and requires every member, which is
  what `--mode same` already did.
- **A failed extraction left its target directory behind.** Vetting guarantees
  a *rejected* archive creates nothing, but an archive whose central directory
  is intact and whose local file headers are damaged passes vetting and dies
  inside `extractall`. Found in production: an empty directory had been sitting
  beside a corrupt 75 MB archive since July. The directory is now removed with
  `rmdir`, which the kernel refuses to apply to anything non-empty — so the
  cleanup can take back an empty directory this run created, and can never take
  a file. A pre-existing directory and the watched folder itself (what
  `--mode same` targets) are both excluded outright. A partial extraction is
  kept and reported as incomplete rather than deleted.
- Preset keys were checked for classification but never for wiring. A key
  could sit in `PRESET_KEYS`, pass the guard, and still be ignored when a
  preset was loaded — the same shape as the original `--no-clobber` bug the
  guard was written for. The guard now reads `cli.py` and proves each key is
  taken off the config, and one preset is round-tripped end to end through the
  real CLI. That round-trip found the `--once` bug above on its first run.

### Added

- **`exec` action** — run any command when a file finishes landing, with
  `{path}` / `{dir}` / `{name}` / `{stem}` / `{ext}` / `{rel}` substituted into
  it. Settle detection, the done-store, the retry queue, `status` and the
  LaunchAgent were always action-agnostic; reaching them used to mean adding
  Python to this repo. There is no shell — the template is split into argv once
  and placeholders go into the resulting tokens, so a file named
  `note; rm -rf ~.mp4` is an argument rather than a second command. Exit 0 is
  done, 75 asks to be retried later (`EX_TEMPFAIL`), anything else is a
  permanent failure with the command's own stderr quoted in the log. A default
  one-hour timeout stops a hung command from silently stopping the watcher.
- **`unzip` notifies**, like `copy` has since July, with Show in folder. A
  failure says the archive was kept — otherwise a folder where nothing has
  happened and one where something went wrong look identical.
- **`convx --passthru`** appends extra arguments verbatim, so a flag ConvX
  grows tomorrow is usable the day it ships rather than the day a release here
  catches up.

## [0.1.0] - 2026-08-15

### Added

- **Watch loops.** Watch a folder over FSEvents (or `--backend poll`), wait
  for each file to finish being written — write handles closed and size
  stable — then run an action. `--once` for a single pass, `--recursive` for
  nested trees, `--catch-up` to process what was already there.
- **Actions.** `copy` (atomic, mirrors subdirectories, never deletes the
  source) and `unzip` (deletes the archive only after every member is verified
  present).
- **Wrapping another tool.** A second kind of action that hands the folder to
  a program which does its own watching, and supervises it — presets, pidfile,
  LaunchAgent and clean child termination, without a second settle loop on top
  of theirs. `convx` is the first, wrapping [ConvX](https://github.com/JSBtechnologies/convx).
- **Presets.** `--save-preset` (merges into an existing preset rather than
  resetting the fields it did not mention), `--list-presets` (reports each
  preset's health instead of rendering a dead one identically to a live one),
  and `presets forget` (dry by default; refuses while a watcher holds it).
- **Persistent done-state**, so a restart does not redo finished work, and
  `--pending` to report files that baseline seeding skipped with no output to
  show for it — read-only, so it is safe to point at a live folder.
- **Retry queue** for transient failures, with exponential backoff and a 72h
  give-up budget. A destination coming back produces no event on the source,
  so without this a copy that failed on an unmounted drive was never retried.
- **Operations.** `status` (running watchers with a log tail), `prune`,
  `service install|uninstall|status` for LaunchAgents, structured logging with
  `--json`, `--dry-run`, and packaging with console scripts on `PATH`.

### Fixed

- **`lsof` was unfindable under launchd**, whose default `PATH` omits
  `/usr/sbin`. Every file looked permanently open, so watchers ran healthily
  and processed nothing. Now resolved by absolute path, and a missing `lsof`
  degrades instead of deadlocking.
- **Archives could lose members silently.** Case-colliding and
  unicode-normalisation-colliding entries overwrote each other, and then the
  archive was deleted. Both are now refused.
- **Recursive unzip flattened the tree**, destroying both archives when two
  shared a name in different directories.
- **Stalled files were abandoned outright.** Releasing a write handle does not
  reliably emit an FSEvent, so that discarded finished work. A held-open file
  is now re-checked slowly and only abandoned after a full day.
- **The poll backend never serviced the retry queue**, so it re-ran permanent
  failures on every scan with no backoff and no budget.
- **A same-size re-export was mistaken for the file already copied**, so the
  destination kept the stale version with nothing in the log to say why.
- **A file below `--min-bytes` announced itself on every scan, forever** —
  around 34,000 log lines a day per file at the default poll, from two lines
  that were each meant to be said once. `progress` is now emitted only when
  the size actually moves, so a file climbing towards the threshold still
  reports and a parked one goes quiet.
- `--once` exited 0 even when every copy failed, so cron and CI could not tell.
- Log injection through filenames, pidfile collisions between watchers sharing
  a name, and a wrapped child outliving its wrapper.

### Performance

- `lsof` is consulted only once the size has plateaued — a growing file is
  already proof of an open write handle, and `stat` answers that for a
  fraction of the cost.
- The done-store no longer re-reads and re-parses itself on every write, which
  made a growing store quadratic in the common single-writer case.

### Testing

- `core/watch.py` — the event loop, where every audit round found its bugs —
  is covered at 96% by tests with no timing dependency, through a harness that
  substitutes a virtual clock, a stub observer and a scripted `lsof` and
  leaves everything else real.
- `make check` runs lint, types and tests on every commit via a versioned
  pre-commit hook; GitHub Actions runs the same on macOS across Python 3.10
  and 3.13.

[Unreleased]: https://github.com/medazizktata/folder-watch/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/medazizktata/folder-watch/releases/tag/v0.2.1
[0.2.0]: https://github.com/medazizktata/folder-watch/releases/tag/v0.2.0
[0.1.0]: https://github.com/medazizktata/folder-watch/releases/tag/v0.1.0
