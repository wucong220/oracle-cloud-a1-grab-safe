#!/usr/bin/env python3
"""Offline checks. Never locates/runs OCI; all OCI operations use tests/mock_oci.py."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELLS = ["grab.sh", "setup.sh", "notify.sh", "status.sh", "config.example.sh"]


class Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.subtests = 0

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({"test": test.id(), "result": "PASS"})

    def addSubTest(self, test, subtest, err):
        self.subtests += 1
        super().addSubTest(test, subtest, err)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shellcheck", default=shutil.which("shellcheck"))
    parser.add_argument("--bash", default=shutil.which("bash"))
    parser.add_argument("--report", default=str(ROOT / "audit" / "test-results.json"))
    args = parser.parse_args()
    require = lambda executable: executable and Path(executable).is_file()
    if not require(args.shellcheck) or not require(args.bash):
        print("Bash and ShellCheck are required; checks were NOT completed.", file=sys.stderr)
        return 2
    os.chdir(ROOT)
    start = dt.datetime.now(dt.timezone.utc)
    static = []
    for name, command in [("ShellCheck", [args.shellcheck, "--severity=style", *SHELLS]), *[("bash -n " + name, [args.bash, "--noprofile", "--norc", "-n", name]) for name in SHELLS]]:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        static.append({"check": name, "exit_code": result.returncode, "output": result.stdout + result.stderr})
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    results = unittest.TextTestRunner(verbosity=2, resultclass=Results).run(suite)
    sources = sorted([ROOT / "a1grab.py", *[ROOT / name for name in SHELLS], ROOT / "a1grab.service", *ROOT.joinpath("tests").glob("*.py"), *ROOT.joinpath("tools").glob("*.py"), *ROOT.joinpath("schemas").glob("*.json")])
    report = {"started_at_utc": start.isoformat(), "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "python": sys.version.split()[0], "platform": sys.platform, "oci_transport": "OFFLINE_EXECUTABLE_MOCK_ONLY", "real_oci_launches": 0, "tests_run": results.testsRun, "subtests_run": results.subtests, "failures": len(results.failures), "errors": len(results.errors), "skipped": len(results.skipped), "static_checks": static, "passed": results.wasSuccessful() and all(s["exit_code"] == 0 for s in static), "cases": results.cases, "tested_source_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("passed", "tests_run", "subtests_run", "failures", "errors", "skipped", "real_oci_launches")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
