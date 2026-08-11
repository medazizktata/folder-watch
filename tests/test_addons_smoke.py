#!/usr/bin/env python3
"""Smoke-test folder-watch addons: unzip + convx (wrapper).

  python3 tests/test_addons_smoke.py
  python3 tests/test_addons_smoke.py --skip-convx
  python3 tests/test_addons_smoke.py --skip-event
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _isolate  # noqa: F401  (must precede folder_watch — redirects state)

from folder_watch import UnzipAction, build_convx_watch_cmd
from folder_watch import main as fw_main


def fw_cmd(*args: str) -> list[str]:
    """argv for a child folder-watch process."""
    py = ROOT / ".venv" / "bin" / "python"
    return [str(py) if py.is_file() else sys.executable, "-m", "folder_watch", *args]


def fw_env() -> dict[str, str]:
    """Env for a child folder-watch process (package must be importable)."""
    env = dict(os.environ)
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{ROOT}:{prev}" if prev else str(ROOT)
    return env


def write_tiny_png(path: Path) -> None:
    def chunk(t: bytes, d: bytes) -> bytes:
        return (
            struct.pack(">I", len(d))
            + t
            + d
            + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        )

    raw = b"\x00" + b"\x00\x00\x00"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def test_unzip() -> list[str]:
    _section("unzip (named / same / to)")
    fails: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="fw-unzip-"))
    try:
        inbox = tmp / "inbox"
        inbox.mkdir()
        out = tmp / "out"
        out.mkdir()

        z = inbox / "demo.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "hello")
        r = UnzipAction(mode="named").run(z)
        if r != "extracted" or not (inbox / "demo" / "a.txt").is_file() or z.exists():
            fails.append("named")
            _fail(f"named → {r}")
        else:
            _ok("named → inbox/demo/ + zip deleted")

        z2 = inbox / "flat.zip"
        with zipfile.ZipFile(z2, "w") as zf:
            zf.writestr("b.txt", "x")
        r = UnzipAction(mode="same").run(z2)
        if r != "extracted" or not (inbox / "b.txt").is_file() or z2.exists():
            fails.append("same")
            _fail(f"same → {r}")
        else:
            _ok("same → extract in place + zip deleted")

        z3 = inbox / "pack.zip"
        with zipfile.ZipFile(z3, "w") as zf:
            zf.writestr("c.txt", "y")
        r = UnzipAction(mode="to", to=out).run(z3)
        if r != "extracted" or not (out / "pack" / "c.txt").is_file() or z3.exists():
            fails.append("to")
            _fail(f"to → {r}")
        else:
            _ok("to → OUT/pack/ + zip deleted")

        bad = inbox / "bad.zip"
        bad.write_bytes(b"not-a-zip")
        r = UnzipAction(mode="named").run(bad)
        if r != "fail" or not bad.exists():
            fails.append("bad")
            _fail(f"bad zip → {r} kept={bad.exists()}")
        else:
            _ok("bad zip kept on failure")

        z4 = inbox / "cli.zip"
        with zipfile.ZipFile(z4, "w") as zf:
            zf.writestr("d.txt", "z")
        # bad.zip is still sitting in this inbox from the check above, so a
        # correct run extracts cli.zip AND reports failure via the exit code.
        code = fw_main(
            ["unzip", str(inbox), "--mode", "named", "--once",
             "--stable", "1", "--ext", "zip"]
        )
        if not (inbox / "cli" / "d.txt").is_file():
            fails.append("cli")
            _fail(f"CLI unzip --once did not extract, code={code}")
        elif code == 0:
            fails.append("cli-exit")
            _fail("CLI unzip --once exited 0 despite bad.zip failing")
        else:
            _ok("CLI folder-watch unzip --once (extracts, exits non-zero on failure)")

        # And a clean inbox exits 0.
        clean = tmp / "clean"
        clean.mkdir()
        z5 = clean / "ok.zip"
        with zipfile.ZipFile(z5, "w") as zf:
            zf.writestr("e.txt", "e")
        code = fw_main(
            ["unzip", str(clean), "--mode", "named", "--once",
             "--stable", "1", "--ext", "zip"]
        )
        if code != 0 or not (clean / "ok" / "e.txt").is_file():
            fails.append("cli-clean")
            _fail(f"clean CLI unzip --once code={code}")
        else:
            _ok("clean run exits 0")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_convx(skip: bool) -> list[str]:
    _section("convx (wrapper → convx watch)")
    fails: list[str] = []
    if skip:
        print("  SKIP (--skip-convx)")
        return fails
    if not shutil.which("convx"):
        print("  SKIP (convx not on PATH)")
        return fails

    tmp = Path(tempfile.mkdtemp(prefix="fw-convx-"))
    try:
        src = tmp / "src"
        src.mkdir()

        cmd = build_convx_watch_cmd(
            src,
            format="webp",
            filter_exts=["png", "jpg"],
            debounce_ms=200,
            overwrite=True,
        )
        if cmd[1] != "watch" or "-t" not in cmd or "webp" not in cmd:
            fails.append("build-cmd")
            _fail(f"bad cmd: {cmd}")
        else:
            _ok(f"build_convx_watch_cmd → {' '.join(cmd[:6])}…")

        # reject --to / --once
        code = fw_main(["convx", str(src), "--format", "webp", "--to", str(tmp / "out")])
        if code != 2:
            fails.append("reject-to")
            _fail(f"--to should exit 2, got {code}")
        else:
            _ok("rejects --to (in-place only)")

        code = fw_main(["convx", str(src), "--format", "webp", "--once"])
        if code != 2:
            fails.append("reject-once")
            _fail(f"--once should exit 2, got {code}")
        else:
            _ok("rejects --once (use convx convert)")

        # live: folder-watch convx → convx watch; drop png after watcher is up
        import subprocess

        live = Path(tempfile.mkdtemp(prefix="fw-convx-live-", dir="/tmp"))
        try:
            png = live / "tiny.png"
            webp = live / "tiny.webp"
            log = live / "log.txt"
            cmd2 = fw_cmd(
                "convx",
                str(live),
                "--format",
                "webp",
                "--ext",
                "png",
                "--debounce",
                "100",
            )
            with open(log, "w") as f:
                p = subprocess.Popen(
                    cmd2, stdout=f, stderr=subprocess.STDOUT, text=True, env=fw_env()
                )
            # Wait for convx's own banner — a fixed sleep races the FSEvents
            # stream, and a write landing before it is never delivered.
            ready = time.time() + 15
            while time.time() < ready:
                if "Press Ctrl+C to stop." in (
                    log.read_text(errors="replace") if log.exists() else ""
                ):
                    break
                time.sleep(0.1)
            time.sleep(0.3)
            write_tiny_png(png)
            # nudge FSEvents if first write was missed
            time.sleep(0.3)
            png.write_bytes(png.read_bytes())
            deadline = time.time() + 12
            while time.time() < deadline and not webp.is_file():
                time.sleep(0.25)
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()

            text = log.read_text() if log.exists() else ""
            if not webp.is_file() or webp.stat().st_size <= 0:
                fails.append("live-watch")
                _fail(f"wrapper→convx watch no webp\n{text[:600]}")
            else:
                _ok(f"wrapper→convx watch png→webp ({webp.stat().st_size} B) in-place")
                if "folder-watch →" not in text or "convx watch" not in text:
                    fails.append("wrapper-banner")
                    _fail(f"missing wrapper banner\n{text[:300]}")
                else:
                    _ok("prints folder-watch → convx watch …")
                if not png.exists():
                    fails.append("source-kept")
                    _fail("source png deleted")
                else:
                    _ok("source kept")
        finally:
            shutil.rmtree(live, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def test_event_unzip(skip: bool) -> list[str]:
    _section("event-driven unzip (FSEvents)")
    fails: list[str] = []
    if skip:
        print("  SKIP (--skip-event)")
        return fails

    tmp = Path(tempfile.mkdtemp(prefix="fw-event-"))
    inbox = tmp / "inbox"
    inbox.mkdir()
    log = tmp / "log.txt"
    try:
        import subprocess

        cmd = fw_cmd(
            "unzip",
            str(inbox),
            "--mode",
            "named",
            "--backend",
            "event",
            "--debounce",
            "200",
            "--stable",
            "1",
            "--settle",
            "0.2",
            "--ext",
            "zip",
        )
        with open(log, "w") as f:
            p = subprocess.Popen(
                cmd, stdout=f, stderr=subprocess.STDOUT, text=True, env=fw_env()
            )
        time.sleep(0.5)
        z = inbox / "drop.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("ok.txt", "event")
        deadline = time.time() + 8
        dest = inbox / "drop" / "ok.txt"
        while time.time() < deadline and not dest.is_file():
            time.sleep(0.2)
        p.terminate()
        try:
            p.wait(timeout=2)
        except Exception:
            p.kill()
        if not dest.is_file() or z.exists():
            fails.append("event-unzip")
            _fail(f"FSEvents unzip failed log={log.read_text()[:400]}")
        else:
            _ok("FSEvents → unzip named + delete zip")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test unzip + convx addons")
    ap.add_argument("--skip-convx", action="store_true")
    ap.add_argument("--skip-event", action="store_true")
    args = ap.parse_args()

    print("folder-watch addon smoke tests")
    print(f"root: {ROOT}")

    fails = 0
    fails += len(test_unzip())
    fails += len(test_convx(skip=args.skip_convx))
    fails += len(test_event_unzip(skip=args.skip_event))

    print("\n== summary ==")
    if fails:
        print(f"FAILED ({fails})")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
