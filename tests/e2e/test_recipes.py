"""The README's `exec` recipes, run for real so the documented commands stay
true. Each one starts a watcher with the recipe's --cmd, drops a file, and
checks what the command left behind. A recipe whose tool is missing skips.
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import unittest

from fixtures import WatcherCase


class TestRecipes(WatcherCase):
    def run_recipe(self, cmd: str, name: str, *extra: str) -> None:
        self.start("exec", str(self.src), "--cmd", cmd, *extra)
        self.drop(name, size=1000)
        self.wait_for("ran", name=name)

    def test_move_to_another_folder(self):
        self.run_recipe(f"/bin/mv -n {{path}} {self.dest}/", "a.pdf")

        self.assertTrue((self.dest / "a.pdf").is_file())
        self.assertFalse((self.src / "a.pdf").exists())

    def test_rename_with_the_date_into_another_folder(self):
        self.run_recipe(
            "/bin/sh -c 'mv -n \"$1\" \"$2/$(date +%F)-$(basename \"$1\")\"' "
            f"sh {{path}} {self.dest}",
            "scan.jpg",
        )

        today = datetime.date.today().isoformat()
        self.assertTrue((self.dest / f"{today}-scan.jpg").is_file())

    def test_a_shell_recipe_does_not_run_the_file_name(self):
        """The file name reaches the script as $1, never as script text. The
        command runs in the file's own folder, so a `touch pwned` that ran
        would leave its marker there."""
        name = "x;touch pwned;.txt"
        self.run_recipe(f"/bin/sh -c 'cp \"$1\" \"$2\"/' sh {{path}} {self.dest}", name)

        self.assertTrue((self.dest / name).is_file())
        self.assertFalse((self.src / "pwned").exists(), "the file name was executed")

    @unittest.skipUnless(shutil.which("rclone"), "rclone not installed")
    def test_rclone_upload_with_retry_on_failure(self):
        # A local path stands in for a remote such as `drive:Inbox`.
        self.run_recipe(
            f"/bin/sh -c '{shutil.which('rclone')} copy \"$1\" \"$2\" || exit 75' "
            f"sh {{path}} {self.dest}",
            "upload.bin",
        )

        self.assertTrue((self.dest / "upload.bin").is_file())
        self.assertTrue((self.src / "upload.bin").is_file(), "copy, not move")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_video_proxy(self):
        ffmpeg = shutil.which("ffmpeg")
        clip = self.tmp / "clip.mp4"
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
             "testsrc=duration=1:size=1280x720:rate=25", str(clip)],
            check=True,
        )
        self.start(
            "exec", str(self.src), "--ext", "mp4", "--cmd",
            f"{ffmpeg} -nostdin -v error -y -i {{path}} -vf scale=-2:540 "
            f"-c:v libx264 -preset veryfast -crf 28 {self.dest}/{{stem}}_proxy.mp4",
        )
        clip.rename(self.src / "clip.mp4")
        self.wait_for("ran", name="clip.mp4", timeout=60)

        probe = subprocess.run(
            [ffmpeg, "-nostdin", "-i", str(self.dest / "clip_proxy.mp4")],
            capture_output=True, text=True, check=False,
        ).stderr
        self.assertIn("960x540", probe)


if __name__ == "__main__":
    unittest.main()
