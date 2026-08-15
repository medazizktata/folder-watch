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

[Unreleased]: https://github.com/medazizktata/folder-watch/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/medazizktata/folder-watch/releases/tag/v0.1.0
