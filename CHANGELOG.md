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

## [0.3.3] - 2026-11-05

### Fixed

- **A followed folder lost files.** The watcher that restarted on a moved
  folder started from an empty done-store (it is named after the folder's
  path) and took a fresh baseline, so a file dropped between the move and the
  restart (up to 5 s, the move check's interval) was skipped as already
  there, and a file waiting to be retried for its destination was dropped. It
  now takes over the old done-store and scans as after a restore: those files
  are processed, and nothing handled or already there before is redone.

## [0.3.2] - 2026-11-05

### Added

- **`route`: one folder, a different action per kind of file.** A preset's
  `rules` send zips to unzip, videos to a Drive folder and the rest elsewhere,
  first match wins. Each rule is validated as a preset of its own.
- **Dashboard: Test button** — a dry run of a preset against its folder now,
  showing what it would do, changing nothing.
- **Dashboard: History** — files done and failed per preset per day, 30 days.
- CI and releases run on Apple-silicon and Intel macOS runners. A release
  still goes out when one pool has no machine free: publish waits until
  GitHub gives up on that leg (about 15 minutes in practice), then releases
  the build from the other. `gh workflow run release.yml` is a dry run.

### Fixed

- **The dashboard's Edit broke on a folder next to your home.**
  `/Users/me-archive` was shown as `~-archive` (another user's home, to the
  shell), so saving failed with "Could not determine home directory". Home is
  now shortened only where it is a whole path, and Edit sends only the fields
  you changed, so an untouched folder is not re-resolved (a symlink used to
  be swapped for its target).
- `prune` removes only save temps older than an hour; a fresh one may be a
  save about to be renamed into place.
- **A webhook URL sat in a world-readable `presets.json`.** The file keeps
  the mode you gave it, but once a preset holds a webhook it is written
  readable by you only.
- **A route rule without `ext` took every file**, hiding the rules after
  it. Each rule must now say `ext`, with `[]` for every file.
- **A service restarted every 10 seconds while its volume was ejected.** With
  the job loaded while the volume was mounted, launchd kept treating the
  ejected source as present, so a watcher that exited for it (after a
  `kickstart -k` or an upgrade) was started again and again. Under launchd,
  a watcher whose source's volume is ejected now waits for it instead of
  exiting; a missing folder on a mounted disk still exits once.
- **`route` and `exec` presets never retried anything.** The done-store and
  retry queue were only built for copy and unzip, so a route rule's failed
  copy was dropped, and an `exec` command's exit 75 ("retry later") was
  logged and then forgotten, though the README promised both. They now get
  the same done-store and 72-hour retry queue as copy.
- **An empty `dashboard.key` let anyone in.** The key file read as empty
  (created but never written, say) matched a request with no cookie at all.
  A key shorter than 16 characters is now replaced with a new one, the key is
  synced to disk before it is linked into place, and an empty cookie never
  matches. A non-ASCII `?key=` is refused instead of crashing the request.
- **The dashboard's cookie reached every program on 127.0.0.1.** Browsers
  send a cookie to every port of a host, so any local program that got your
  browser to open a page on its own port received the key. The dashboard now
  lives under a path derived from the key (`http://127.0.0.1:8765/` plus 16
  characters) and its cookie is scoped to that path. The plain address still
  takes the `--link` key, or a cookie from before, swaps it for the new one,
  expires the old one, and sends you on: bookmark the address it lands on.
- **A dry run left traces as if it had run.** A `--dry-run` (and so the
  dashboard's Test button) recorded its failures in the audit trail, where
  the History chart counted them, posted them to the webhook, and wrote the
  volume note retention keeps. It now only logs what it would do.

## [0.3.1] - 2026-11-05

### Fixed

- **A source on a volume mounted outside `/Volumes` counted as deleted once
  ejected**, so `prune` would eventually offer to remove its preset. The
  watcher and each `prune` now note the mount a source sits on while it
  exists; a source last seen on a volume that is no longer mounted is not
  counted. Checked with a disk image that `hdiutil` mounted on a folder of
  its own and removed on eject.
- **The dashboard could hang at start on a machine with slow DNS**: Python's
  HTTP server looked up the host's full name, which nothing used. Seen on CI
  runners.

### Testing

- `make feature` red-checks new e2e tests too, against `BASE`'s code.
- A crash-recovery test that misses its kill now says whether the copy
  finished first or never started.

## [0.3.0] - 2026-11-04

### Fixed

- **The unit tests posted real notifications.** A run sent twelve through
  `alerter`, and some tests asked the real launchd. The unit suite now finds
  stubs for every notifier, Finder and `launchctl` first on `PATH`, and a
  harness test fails if it does not. The e2e suite's launchd jobs moved to
  their own label prefix, `com.folder-watch-e2e`.
- **`make feature` counted a test as red when it merely could not run.** It
  now separates tests that fail by assertion on `BASE` from ones that only
  raise there, and matches each test by its exact name rather than as a
  substring of others.
- **A service did not come back when its volume was mounted again.**
  launchd does not re-check PathState on a mount, so a watcher whose source
  sits on a drive stayed stopped after the drive returned. Service plists now
  also carry `StartOnMount`; reinstall a service (`service install`) to get it.
- **`presets edit` on an unreadable `presets.json` said the preset did not
  exist** (exit 1). It now says the file cannot be read, exit 2, and leaves it
  as it was.
- **A service installed with `FOLDER_WATCH_*` overrides set ran on the real
  config**, because launchd passes on no environment. The overrides that are
  set now go into the plist, for watchers and for the tool's own jobs.
- **A `--once` run with many failures showed only the first.** The others
  were held for the hour's summary, whose timer died with the process. The
  summary is now sent as the process exits.
- **A folder deleted from a mounted drive read as "volume not mounted".**
  Anything under `/Volumes` was judged by its path alone; when the drive is
  mounted and only the folder is gone, `status` now says it is missing and
  retention counts it. The docs no longer claim every unmounted volume is
  recognised: one mounted outside `/Volumes` and CloudStorage looks deleted
  once ejected.
- **Every `service install` and `uninstall` left a permanent launchd record.**
  Both ran `launchctl enable`, whose state outlives reboots, for every label;
  they now do so only to clear an actual pause.
- **`audit --since` with a time zone crashed.** `--since
  2026-11-01T00:00:00+01:00` raised TypeError against the trail's local
  times; it is now read as that moment.
- **An entry recorded while the audit trail was being trimmed was lost.**
  Trim rewrites the file; an append in between went to the copy being
  replaced. Appends and trim now share a lock.
- **`doctor` misnamed services whose preset has a space.** It read the name
  back from the launchd label, where `blind photos` is `blind-photos`, and
  reported the preset as gone. It now reads the plist's `--preset`.
- **Any account on the Mac could use the dashboard.** The token that
  guarded its buttons was written into the page, and the page was served to
  anyone who asked 127.0.0.1 for it, so another user (or a page on another
  localhost port) could read the token, then pause services or point a
  preset at another folder. The dashboard now needs a key from a file only
  you can read: `folder-watch dashboard` opens a link carrying it once, the
  page trades it for an HttpOnly, SameSite=Strict cookie, and every page and
  API request needs that cookie. Actions must also come from the page's own
  origin, and the page cannot be framed. New: `dashboard --link`.
- **A webhook's token was written to the audit trail and printed by edits.**
  `presets edit` and `--save-preset` printed the old and new URL in full,
  and the audit trail recorded both, in a file anyone on the Mac could
  read. Both now show only the host, as `presets show` already did, and
  `audit.jsonl` is owner-only (0600), including one written before.
- **A preset could hold values no watcher can use.** `presets edit job to=`
  stored the current folder; `stable=-5`, `poll=nan`, `poll=0` and
  `action=bogus` were saved as typed, and a destination inside a recursive
  source was only refused when the watcher started. Saving, editing,
  `--save-preset` and following a moved folder now check the values first
  and change nothing when one is wrong. `service install` refuses a preset
  that does not exist or does not check out, rather than writing a
  LaunchAgent that fails on every login. A watcher started from a bad
  hand-written preset exits with what to fix, and `status` shows it as an
  error. A refused webhook URL is no longer repeated in the error, since
  Slack and Discord keep the secret in it.
- **Saves to `presets.json` could undo each other.** Each save read the file,
  changed one preset and wrote the lot back, so two saves at once (two
  presets following a renamed folder, the dashboard and the CLI) dropped one
  change, while the audit trail said it was made. Saves now hold a lock for
  the read, the change and the write.
- **A symlinked `presets.json` was replaced by a private copy.** Saving
  renamed the new file over the link. It now writes to the file the link
  points at and keeps that file's permissions.
- **A dead drive buried the screen in notifications.** Every failed file
  notified, again on every retry pass. Failure notifications are now grouped
  per hour: the first notifies at once, the rest are summed up in one
  notification when the hour ends.
- **`--no-notify` still raised the move prompt.** When a watcher's source
  moved, it sent a notification, or for a preset the hour-long "Watch new
  path" prompt, even with `--no-notify`. A move is now only logged then.
- **Saving a preset into an unreadable `presets.json` deleted every other
  preset.** When the file did not parse (half-written or corrupted), saving
  started from an empty set and wrote the file back holding only the new
  preset. It now refuses with exit 2 and leaves the file untouched. Presets
  are also written through a temp file and a rename, so a crash mid-save can
  no longer leave the half-written file that started this.
- **Logs grew without limit, and a LaunchAgent wrote every event twice.**
  `--log` now rotates at 10 MB and keeps 3 old copies. With a log file and no
  terminal, which is how a LaunchAgent runs, events go only to the log file:
  launchd's `.out` file used to receive a full second copy of them.
- **A service whose source folder was gone restarted every ten seconds,
  forever.** The plist set a bare `KeepAlive`, so a preset whose `src` had been
  moved or deleted exited with `src_missing`, and launchd started it again
  every ten seconds. Each run appended the same error to the `--log` file and
  the `.out` file. Two services ran like this for months. Their logs reached
  1.1 million lines (114 MB), almost all of them the same error repeated. One
  of the two was a `convx` preset, which loops the same way. `service install`
  now ties `KeepAlive` to the preset's source folder (`PathState`). A missing
  folder costs one start and one error line, and launchd starts the watcher
  again by itself when the folder comes back (a remounted volume needed
  `StartOnMount`, added later). If
  you change a preset's `src`, run `service install` again so the plist follows
  it. Services installed before this fix keep the old plist until you
  reinstall them.
- **A source that never came back logged once a minute for the life of the
  process.** That is 1,440 lines a day. The gap between `src_waiting` lines now
  doubles from one minute up to one day.

- **A destination inside a recursively watched folder copied itself forever.**
  `copy SRC --to SRC/done --recursive` copied each file into `done/`, which
  raised an event, which copied it into `done/done/` — one level per second,
  unbounded. 29 copies and 28 levels deep in 12 seconds, with every log line
  reading `copied` while the disk filled. Now refused at startup with an
  explanation; there is no safe version of that arrangement.
- **A copy killed mid-flight leaked a full-size hidden file that nothing ever
  removed.** The stale sweep only looked for temps named after *the file being
  copied*, so debris from a render nobody copies twice was never found — at
  full size, invisible in Finder, untouched by `prune`. Temps now carry a fixed
  marker, so the sweep finds all of the tool's own debris in a destination
  regardless of which file it came from, and can never match a file it did not
  write. Leftovers under the previous naming are swept too.
- **The webhook lost whatever was still queued at exit.** Delivery happens on
  a daemon thread — which is what stops a slow endpoint holding up a copy — and
  a daemon thread dies with the process. Under `--once`, the documented cron
  mode, the run's own result was the thing most likely to be dropped. The queue
  is now drained on the way out.
- **No webhook failure was ever reported.** A dead endpoint, an HTTP 500 and a
  connection reset were indistinguishable from success. Undelivered events are
  now counted and reported, and a URL that is not `http(s)://` is refused at
  startup rather than failing silently once per event.
- **`--save-preset` silently dropped `--webhook`**, even though the loader
  reads that key back.
- **`--list-presets` and `presets show` printed webhook URLs in full**,
  including the token — against the guarantee made for the log. Only the host
  is shown now.
- `--help`: four flags were attached to the top-level parser instead of their
  groups, so they printed above the groups and left "actions: exec" showing
  only `--cmd`. `--list-presets` and `--list-actions` had no help text.
- The corrupt-copy notification and the README both said a failed `--verify`
  "will retry", which is untrue under `--once` — there is deliberately no queue
  there, and the failure surfaces as a non-zero exit instead.
- **SIGTERM could orphan the pidfile it exists to manage.** `run_watch`
  installs a handler turning SIGTERM into `SystemExit` precisely so the
  `finally` clears the pidfile — but the write happened *before* that `try`,
  so a signal arriving during it raised out of an uncovered frame and the file
  survived the process. launchd stopping a service moments after starting it
  lands exactly there, and `launchctl kickstart -k` does it by construction.
  Reproduced 10/10, now 0/10.
- **Crash debris was mistaken for a done-store.** The unpublished temp from a
  save was named `done-XXXXXX.json` and `prune` globs `done-*.json`, so a
  process killed mid-write left a file the next tidy-up read as somebody's
  store. Temps are now `.json.tmp`, skipped by that glob, and `prune` removes
  them as the debris they are. Same for the retry queue.

### Added

- **Audit trail** (`audit.jsonl`, `folder-watch audit`, the dashboard's Audit
  button): every file outcome, folder move, preset save (with what changed)
  and removal, service change and cleanup, each with what triggered it (cli,
  service, weekly, dashboard, notification, automatic). Append-only, never
  rotated, a year kept.
- **Dashboard extras**: Run doctor inline; Clean up as a dry run with an Apply
  button; a Log toggle with each preset's last 20 lines; and `dashboard
  --install` to run it at login on a fixed port (`--strict-port`, retried every
  five minutes if the port is taken).
- **`presets edit NAME KEY=VALUE … [--unset KEY]`**: change a preset without
  hand-editing JSON. Values are typed like the flags, paths stored absolute,
  and an installed service is reinstalled so its plist follows. The dashboard
  gains an **Edit** button for the watched folder and copy destination.
- **`follow_moves`** per preset (`--follow-moves` with `--save-preset`):
  a moved source is followed without the button, except into a Trash, which
  falls back to it. (A move to another volume is a copy and a delete, which
  reads as a deleted folder, not a move.) It also works with `--no-notify`,
  silently.
- **`folder-watch digest`** and a weekly digest notification: what each preset
  did in the last 7 days ("copied 34, 2 failed (photos, inbox)"), counted from
  its logs. Log lines now carry the date (`YYYY-MM-DD HH:MM:SS` in plain
  logs, a `ts` field with `--json`), which is what makes a week countable.
- **`folder-watch dashboard`**: `status --all` as a live local web page,
  refreshed every 5 seconds, with Pause / Resume, Open folder and Open log
  buttons. Bound to 127.0.0.1; actions need a per-run token, and other host
  names are refused. `status --all` now shows paths from `~`, sizes in
  B/KB/MB, and "-" for a job that has not exited.
- **`folder-watch status --all`**: one table of every preset with its health,
  its service (running, paused, not loaded), last exit code, last result read
  from its log, and log size.
- **`service pause` / `service resume`** stop a service and keep it stopped
  across logins without uninstalling it. `install` and `uninstall` clear a
  pause, which launchd otherwise remembers after the plist is gone.
- **`--reveal`** opens Finder on each result as it lands (the copy, the
  extracted folder, or the file a command ran on) without a click, whether or
  not notifications are on. It can be saved in a preset, so services can use
  it, and it does nothing on a dry run.

- **`folder-watch doctor`** checks the things a watcher cannot check for
  itself: notifiers installed, every service loaded and its last exit code,
  destinations writable with space left, and whether launchd can read the
  Desktop, Documents and Downloads folders a preset uses, probed with the
  interpreter the services run. The weekly job runs the service and
  destination checks and notifies on failure, so a service that dies before
  folder_watch loads is no longer restarted forever unnoticed.

- **Retention for dead presets.** `prune` now also removes a preset whose
  source has been deleted or renamed for 30 days, along with its LaunchAgent
  and default logs. It never counts a source on an unmounted volume under
  `/Volumes` or `~/Library/CloudStorage`, and never
  removes a preset a watcher is running. The clock starts at the first check
  that sees the folder gone (`dead-presets.json`) and resets if the folder
  returns. `prune` also removes logs in the state folder that no preset owns
  and that have not been written for 30 days, and cuts launchd `.out` logs
  over 10 MB down to their last 1 MB. `prune --schedule` installs a weekly job
  that sends one notification listing all of it, with a **Clean up** button;
  nothing is removed without the click. Without this, a preset whose project
  ended stayed forever, and a LaunchAgent left installed for it looped.

- **A moved source is detected, not waited for.** A watcher used to treat a
  renamed folder like a deleted one and wait for the old name to return,
  logging `src_waiting` every minute, indefinitely. On macOS it now holds the
  folder open, finds where it went (`F_GETPATH`), logs
  `error reason=src_moved now=…`, exits 2 and notifies. For a stored preset
  the notification offers **Watch new path**, which saves the new `src` and
  restarts the watcher on it, reinstalling the LaunchAgent when it runs under
  one. Nothing follows a move unasked: the new place can be the Trash.
- **`service status` warns when the plist and the preset disagree** about the
  source folder, which happens when `src` is edited after `service install`.

- **`--verify`** re-reads both files after a copy and compares sha256. The
  size check catches a truncated write and nothing else; this catches the
  right number of wrong bytes, which is rare on local disk, less rare across a
  network mount, and silent. A copy that fails is deleted rather than left
  looking finished, and goes back on the retry queue. Off by default — it
  costs a full read of each file.
- **`--webhook URL`** POSTs outcome events as JSON. Hooked onto the log rather
  than each action, so a new action is covered automatically. It cannot block
  the watch loop (bounded queue, daemon thread, drops and counts under load),
  cannot crash the watcher, and never puts the URL in a log — endpoint URLs
  carry the secret in the path.
- **`presets show NAME`** — one preset in full, including which file each
  value came from. A preset defined in two files is merged and nothing said so.
- `--help` is grouped by action instead of listing 45 flags in one block.

### Testing

- **A test class could no longer fail.** A helper named `fail` replaced
  `TestCase.fail`, which every assert method calls to report a mismatch; a
  deliberate break exposed it. A harness rule now refuses any test class that
  shadows a `TestCase` method, checked against that exact case.
- **`exec` recipes, run for real** (`tests/e2e/test_recipes.py`): move, rename
  with the date, rclone upload with retry, and a 540p ffmpeg proxy, plus a
  check that a file name passed as `$1` is never executed (calibrated against
  the unsafe form, which does execute it). The e2e fixture now waits for the
  watcher's baseline, not just its start: a file landing between the two was
  taken as already there and skipped, which failed one run in five.
- **End-to-end tests** (`tests/e2e`, `make e2e`) with reusable fixtures for
  real watchers and real launchd jobs. Each test was checked against the
  break it exists for: a bare `KeepAlive`, move detection switched off, and
  `doctor` ignoring exit codes.
- **`make feature`**, a per-feature gate: everything `make all` runs, with the
  bytecode cache off, coverage of the changed files, and every new test run
  against the base commit's code, where it must fail.
- **Crash recovery**, 21 tests. A watcher is killed with SIGKILL mid-copy and
  mid-extract, plus surgical kill points a poll cannot aim at: between
  `extractall` and the archive being deleted, and between the bytes landing and
  the done-store recording them — where an inverted statement order would mark
  a file done that was never copied. Also covers state files staying valid JSON
  across a crash mid-write, finished files surviving while only the in-flight
  one is lost, three crash/restart cycles converging, and SIGTERM differing
  from SIGKILL in exactly one visible way. Both fixes above were found here,
  and each was verified by putting the bug back.

## [0.2.2] - 2026-08-16

### Added

- **A sound with the notification**, on by default and deliberately quiet:
  `Tink`, a click rather than an announcement, because this fires once per
  finished file on a folder that can take a batch of them. `--no-sound`
  silences it, `--sound-name NAME` picks another, `--list-sounds` shows what
  the machine has — including anything in `~/Library/Sounds`. It rides along
  with the notification rather than playing separately, so it cannot drift out
  of sync with the popup and turning notifications off silences it too. A name
  matching no sound is reported once instead of passed through: every notifier
  accepts an unknown name and plays nothing, which makes a typo
  indistinguishable from the feature being off. Preset-settable, unlike
  `--notify`, since unattended watchers start from a preset and no other flags.

- `--list-presets` says so when there are none, instead of printing nothing at
  all. Silence reads as "the command did nothing" rather than "there is
  nothing to show".
- `FOLDER_WATCH_LAUNCH_AGENTS` redirects where plists are written, like the
  config and state directories already could. Without it there was no way to
  exercise `service install` without writing into the real
  `~/Library/LaunchAgents`, so the one command that can take a live watcher
  down was the one with no tests.

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

[Unreleased]: https://github.com/medazizktata/folder-watch/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/medazizktata/folder-watch/releases/tag/v0.3.3
[0.3.2]: https://github.com/medazizktata/folder-watch/releases/tag/v0.3.2
[0.3.1]: https://github.com/medazizktata/folder-watch/releases/tag/v0.3.1
[0.3.0]: https://github.com/medazizktata/folder-watch/releases/tag/v0.3.0
[0.2.2]: https://github.com/medazizktata/folder-watch/releases/tag/v0.2.2
[0.2.1]: https://github.com/medazizktata/folder-watch/releases/tag/v0.2.1
[0.2.0]: https://github.com/medazizktata/folder-watch/releases/tag/v0.2.0
[0.1.0]: https://github.com/medazizktata/folder-watch/releases/tag/v0.1.0
