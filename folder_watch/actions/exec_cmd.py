"""Exec action — run a command when a file finishes landing.

This is the generalisation of the whole tool. Settle detection, the done-store,
the retry queue, pidfiles, `status` and the LaunchAgent are the expensive part
and they are entirely action-agnostic; before this, reaching them meant adding
Python to this repo. Now anything with a command line can use them.

Two things are deliberate and load-bearing:

* **There is no shell.** The template is split into argv once, with shlex, and
  the placeholders are substituted into the resulting tokens afterwards. A file
  called `; rm -rf ~.mp4` therefore arrives as one argument to the command, not
  as a second command. Doing it the other way round — substituting into the
  string and splitting after — is the entire bug class, and it is a filename an
  attacker never needs to control: an unlucky export name is enough.
* **The exit code is the contract.** 0 is done, 75 asks to be retried later
  (`EX_TEMPFAIL`, the BSD sysexits convention), anything else is a permanent
  failure. That maps a command onto the same retry queue a failed copy uses,
  without the watcher having to guess what "failed" means for someone else's
  program.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from folder_watch.actions.base import Action
from folder_watch.support.log import emit
from folder_watch.support.notify import notify

# sysexits.h: "temporary failure, indicating something that is not really an
# error. In sendmail, this means that a mailer could not create a connection,
# and the request should be reattempted later."
EX_TEMPFAIL = 75

# Command not found — the shell's convention, and what Popen reports through
# FileNotFoundError. Retrying will not conjure the binary.
EX_NOTFOUND = 127

# A command that never returns stops the watch loop, silently, forever. That is
# the failure this project keeps finding, so it gets a default rather than an
# invitation to think about it later.
DEFAULT_TIMEOUT = 3600.0

# Enough of the command's own output to see why it failed, without a runaway
# process filling the log with megabytes.
OUTPUT_TAIL = 500


class ExecAction(Action):
    name = "exec"
    default_ext = "*"
    default_min_bytes = 1

    def __init__(
        self,
        cmd: str,
        *,
        src_root: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        dry_run: bool = False,
        notify_on_done: bool = False,
    ) -> None:
        if not cmd or not cmd.strip():
            raise ValueError("exec requires a non-empty --cmd")
        self.cmd = cmd
        self.template = shlex.split(cmd)
        if not self.template:
            raise ValueError(f"--cmd is not a runnable command line: {cmd!r}")
        self.src_root = src_root.expanduser() if src_root else None
        self.timeout = timeout
        self.dry_run = dry_run
        self.notify_on_done = notify_on_done

    def describe(self) -> str:
        bits = [f"exec {self.cmd!r}"]
        if self.timeout:
            bits.append(f"timeout={int(self.timeout)}s")
        if self.notify_on_done:
            bits.append("notify")
        if self.dry_run:
            bits.append("dry-run")
        return " ".join(bits)

    def done_mode(self) -> str | None:
        """The command is part of the done-key.

        So editing --cmd re-processes files the previous command already
        handled, which is almost always what changing it meant.
        """
        return self.cmd

    def fields(self, src: Path) -> dict[str, str]:
        """The substitutions available to a template."""
        try:
            path = src.resolve()
        except OSError:
            path = src
        rel = path.name
        if self.src_root is not None:
            try:
                rel = str(path.relative_to(self.src_root.resolve()))
            except (ValueError, OSError):
                pass
        return {
            "path": str(path),
            "dir": str(path.parent),
            "name": path.name,
            "stem": path.stem,
            "ext": path.suffix.lstrip("."),
            "rel": rel,
        }

    def build_argv(self, src: Path) -> list[str]:
        """Substitute into already-split tokens — never into the string.

        This is the ordering that makes a hostile filename inert.
        """
        values = self.fields(src)
        argv: list[str] = []
        for token in self.template:
            for key, value in values.items():
                token = token.replace("{" + key + "}", value)
            argv.append(token)
        return argv

    def run(self, path: Path) -> str:
        argv = self.build_argv(path)
        if self.dry_run:
            emit("would_exec", name=path.name, argv=shlex.join(argv))
            return "would_exec"

        started = time.monotonic()
        # cwd is the file's own folder, so a command that writes a relative
        # output puts it beside the input. Inheriting the watcher's cwd would
        # mean "/" under launchd, which is both surprising and unwritable.
        try:
            proc = subprocess.run(
                argv,
                cwd=str(path.parent),
                capture_output=True,
                text=True,
                timeout=self.timeout or None,
                check=False,
                env=os.environ.copy(),
            )
        except FileNotFoundError:
            emit("fail", name=path.name, reason="command_not_found", cmd=argv[0])
            self._announce(path, "fail")
            return "fail"
        except PermissionError:
            emit("fail", name=path.name, reason="command_not_executable", cmd=argv[0])
            self._announce(path, "fail")
            return "fail"
        except subprocess.TimeoutExpired:
            # Killed mid-flight, so what it did or half-did is unknown.
            # Retrying an unknown state is worse than reporting it.
            emit(
                "fail",
                name=path.name,
                reason="timeout",
                timeout_s=int(self.timeout),
                cmd=argv[0],
            )
            self._announce(path, "fail")
            return "fail"
        except OSError as e:
            emit("fail", name=path.name, reason="exec_error", err=str(e))
            self._announce(path, "fail")
            return "fail"

        took = time.monotonic() - started
        if proc.returncode == 0:
            emit("ran", name=path.name, cmd=argv[0], took_s=round(took, 2))
            self._announce(path, "ran")
            return "ran"

        tail = self._tail(proc)
        if proc.returncode == EX_TEMPFAIL:
            # The command asked for another go. Straight into the same queue a
            # failed copy uses, with the same backoff and give-up budget.
            emit(
                "retry_requested",
                name=path.name,
                exit=proc.returncode,
                note="EX_TEMPFAIL",
                output=tail,
            )
            return "retry"

        emit(
            "fail",
            name=path.name,
            reason=(
                "command_not_found"
                if proc.returncode == EX_NOTFOUND
                else "nonzero_exit"
            ),
            exit=proc.returncode,
            took_s=round(took, 2),
            output=tail,
        )
        self._announce(path, "fail")
        return "fail"

    @staticmethod
    def _tail(proc: subprocess.CompletedProcess) -> str:
        """Why it failed, in the failure line itself.

        A command's own diagnostic is the only thing that explains a bare exit
        code, and sending it to the watcher's stdout instead would interleave
        it with everything else and lose it under launchd.
        """
        text = ((proc.stderr or "") + (proc.stdout or "")).strip()
        if len(text) > OUTPUT_TAIL:
            text = "…" + text[-OUTPUT_TAIL:]
        return text

    def _announce(self, path: Path, result: str) -> None:
        if not self.notify_on_done:
            return
        if result == "ran":
            notify("folder-watch ran", f"{self.template[0]} on {path.name}",
                   reveal=path)
        else:
            notify("folder-watch command failed", path.name, reveal=path)
