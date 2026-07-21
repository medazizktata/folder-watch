# folder-watch

Watch a folder until files finish writing, then run a pluggable **action**.

**Location:** `~/Desktop/folder-watch`  
**Global CLIs:** `~/.local/bin/{folder-watch,watch-copy,watch-unzip,watch-convx}` → symlinks into `bin/`

## How watching works

**Default: `--backend event`** (FSEvents on macOS via `watchdog`) — used by **copy** / **unzip**

1. FS event (`created` / `modified` / `moved`)
2. **Debounce** (`--debounce` ms, default 500) — wait until events quiet
3. **Settle** — `lsof` closed + size unchanged for `--stable` checks at `--settle` interval
4. Run action

**Fallback: `--backend poll`** — timer loop (`--poll` seconds).

**convx** does **not** use this loop — it wraps `convx watch` (ConvX owns FSEvents + convert).

```bash
folder-watch copy ./Out --to ./Drive --backend event   # default
folder-watch copy ./Out --to ./Drive --backend poll --poll 30
folder-watch convx ~/Inbox --format webp --ext png,jpg
```

## Actions

| Action | Behavior |
|--------|----------|
| `copy` | Copy to `--to DIR` (never deletes source) |
| `unzip` | Extract `.zip`; delete zip **only after verified success** |
| `convx` | Thin wrapper → `convx watch` (in-place convert) |

### Unzip modes (default: `named`)

| Mode | Result for `foo.zip` |
|------|----------------------|
| `named` | `./foo/` then delete zip |
| `same` | extract into watch folder, delete zip |
| `to` | `--to DIR` → `DIR/foo/`, delete zip |

### ConvX notes

- Requires `convx` on PATH (`~/.local/bin/convx`)
- `folder-watch convx …` ≈ `convx watch DIR -t FORMAT --filter …`
- **In-place only** — no `--to OUTDIR` (use `convx convert -d` for that)
- `--ext png,jpg` → ConvX `--filter *.png,*.jpg`
- `--format` → ConvX `-t`; `--convx-preset` → ConvX `-p`
- `--debounce` / quality / width / fps / max-size / overwrite passed through
- No `--once` / `--delete-source` (not part of `convx watch`)

## Usage

```bash
folder-watch copy ./Output --to ./Drive/V1 --ext mp4
folder-watch unzip ~/Downloads/inbox
folder-watch unzip ~/Downloads/inbox --mode same
folder-watch unzip ~/Downloads/inbox --mode to --to ~/Unpacked

# ConvX (wrapper)
folder-watch convx ~/Inbox --format webp --ext png,jpg,jpeg,heic
folder-watch convx ~/Inbox --format mp3 --ext wav,aiff --quality 90
folder-watch --preset png-to-webp

folder-watch --preset la-chaise-v1

# wrappers
watch-copy ./Output ./Drive/V1
watch-unzip ~/Downloads/inbox
watch-convx ~/Inbox --format webp --ext png,jpg
```

Presets: `~/.config/folder-watch/presets.json`

## Tests

```bash
cd ~/Desktop/folder-watch
.venv/bin/python -m unittest discover -s tests -v
./bin/test-addons
./bin/test-addons --skip-convx
./bin/test-addons --skip-event
```

## Install / re-link

```bash
ln -sf ~/Desktop/folder-watch/bin/folder-watch ~/.local/bin/folder-watch
ln -sf ~/Desktop/folder-watch/bin/watch-copy   ~/.local/bin/watch-copy
ln -sf ~/Desktop/folder-watch/bin/watch-unzip  ~/.local/bin/watch-unzip
ln -sf ~/Desktop/folder-watch/bin/watch-convx  ~/.local/bin/watch-convx
```
