"""Prove the original DRY_RUN bug using a fake Bash OCI command, offline."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = "b6bc5cb20e0d8f5e51e4a4c1691c5573ef3c9e61"


def bash_path():
    known = Path("C:/Program Files/Git/bin/bash.exe")
    return str(known) if known.is_file() else shutil.which("bash")


class OriginalRegression(unittest.TestCase):
    def test_original_dry_run_calls_launch_against_mock(self):
        bash = bash_path()
        self.assertIsNotNone(bash, "Bash is required; do not skip the baseline regression")
        result = subprocess.run(["git", "show", f"{BASE}:grab.sh"], cwd=ROOT, capture_output=True, check=True)
        with tempfile.TemporaryDirectory(prefix="a1grab-original-mock-") as temp:
            path = Path(temp)
            (path / "grab-original.sh").write_bytes(result.stdout)
            (path / "test.pub").write_text("ssh-ed25519 SYNTHETIC_PUBLIC_TEST_DATA")
            (path / "config.sh").write_text('COMPARTMENT="synthetic"\nADS=("TEST:AP-TOKYO-1-AD-1")\nSUBNET="synthetic"\nIMAGE="synthetic"\nSSH_PUBKEY_FILE="$BASE/test.pub"\n')
            mockbin = path / "bin"
            mockbin.mkdir()
            mock = mockbin / "oci"
            mock.write_text('#!/usr/bin/env bash\nset -eu\nif [[ " $* " == *" compute instance launch "* ]]; then\n printf "MOCK_LAUNCH\\n" >> "$A1GRAB_HOME/mock-writes"\n printf "ocid1.instance.oc1.ap-tokyo-1.synthetic\\n"\nelse\n exit 99\nfi\n', encoding="utf-8")
            mock.chmod(0o700)
            env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
            if os.name == "nt":
                posix_bin = "/" + str(mockbin).replace(":", "").replace("\\", "/")
                posix_temp = "/" + temp.replace(":", "").replace("\\", "/")
                env["PATH"] = f"{posix_bin}:/usr/bin:/bin"
                env["A1GRAB_HOME"] = posix_temp
                env["HOME"] = posix_temp
            else:
                env["PATH"] = f"{mockbin}:/usr/bin:/bin"
                env["A1GRAB_HOME"] = temp
                env["HOME"] = temp
            env["DRY_RUN"] = "1"
            completed = subprocess.run([bash, "--noprofile", "--norc", "./grab-original.sh"], cwd=path, env=env, capture_output=True, timeout=15)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual((path / "mock-writes").read_text().splitlines(), ["MOCK_LAUNCH"])

    def test_current_shell_wrappers_work_with_windows_paths(self):
        bash = bash_path()
        self.assertIsNotNone(bash)
        with tempfile.TemporaryDirectory(prefix="a1grab paths test-") as temp:
            target = Path(temp) / "local config.sh"
            env = dict(os.environ, A1GRAB_PYTHON=sys.executable)
            result = subprocess.run([bash, "--noprofile", "--norc", str(ROOT / "setup.sh"), "--config", str(target)], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.is_file())
            notification = subprocess.run([bash, "--noprofile", "--norc", str(ROOT / "notify.sh"), "secret-sentinel"], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(notification.returncode, 0)
            self.assertNotIn("secret-sentinel", notification.stdout + notification.stderr)


if __name__ == "__main__":
    unittest.main()
