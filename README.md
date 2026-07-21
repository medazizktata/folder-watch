# folder-watch

Watch a folder until files finish writing, then run a pluggable **action**.

**Location:** `~/Desktop/folder-watch` (local git repo)  
**Global CLIs:** `~/.local/bin/{folder-watch,watch-copy,watch-unzip,watch-convx}` → `bin/`

## How watching works

**Default: `--backend event`** (FSEvents via `watchdog`) — **copy** / **unzip**

1. FS event → **debounce** → **settle** (write-handles closed + size stable)
2. Skip files already finished at startup (**new-only**; use `--catch-up` to process old)
3. Run action; persist done-state (scheme B) so restarts don’t redo work

**convx** wraps `convx watch` (ConvX owns FSEvents).

## Actions

| Action | Behavior |
|--------|----------|
| `copy` | Atomic copy → `--to DIR` (retry on OSError); never deletes source; macOS notif |
| `unzip` | Extract `.zip`; delete zip only after verified success |
| `convx` | → `convx watch` (in-place) |
| `service` | LaunchAgent install / uninstall / status |

## Reliability / logging

```bash
# dry-run (no writes)
folder-watch copy ./Out --to ./Drive --once --dry-run

# log file + JSON lines
folder-watch --preset la-chaise-v1 --log ~/.local/state/folder-watch/la-chaise.log --json

# disable persistent done-state
folder-watch copy ./Out --to ./Drive --no-done-store

# copy retries (default 5)
folder-watch copy ./Out --to ./Drive --retries 8

# status of running watchers
folder-watch status
folder-watch status --preset la-chaise-v1
```

Ignores partial/scratch names: `*.part`, `*.tmp`, `*.crdownload`, `._*`, `~$…`, etc.

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

## Tests

```bash
cd ~/Desktop/folder-watch
.venv/bin/python -m unittest discover -s tests -v
./bin/test-addons
```
