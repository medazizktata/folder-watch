# Tests

```bash
cd ~/Desktop/folder-watch
.venv/bin/python -m unittest discover -s tests -v
./bin/test-addons
```

Covers:

- `match_ext` / `parse_exts`
- `CopyAction` (copy, skip, watch once)
- `UnzipAction` modes `named` / `same` / `to`
- bad zip + path-traversal (zip **not** deleted)
- `build_convx_watch_cmd` flag mapping + CLI rejects `--to` / `--once`
- smoke: live `convx watch` via wrapper + event unzip
