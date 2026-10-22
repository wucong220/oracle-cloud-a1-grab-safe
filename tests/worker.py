"""Subprocess worker for real OS locks/restarts, using only executable Mock OCI."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("a1grab", ROOT / "a1grab.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def deny_network(*_args, **_kwargs):
    raise AssertionError("TEST NETWORK ACCESS FORBIDDEN")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network
directory = Path(sys.argv[1]).resolve()
fixture = json.loads((directory / "fixture.json").read_text())
cfg = a.config(directory / "config.sh")
api = a.OCI(cfg, directory, command=[sys.executable, "-I", str(ROOT / "tests" / "mock_oci.py")])
# Only this test worker bypasses installed-runtime attestation for the Mock.
api.verify_dependency_policy = lambda: None
api.verify()
engine = a.Engine(cfg, api, directory, clock=lambda: fixture["now"])
original_save = engine.save


def save(state):
    phase = fixture.get("crash_phase")
    if phase == "after_accept" and state["state"] == "ACCEPTED":
        os._exit(77)
    original_save(state)
    if state["state"] == "IN_FLIGHT":
        if phase == "after_intent":
            os._exit(77)
        if fixture.get("pause_before_launch"):
            (directory / "pause-ready").touch()
            time.sleep(fixture["pause_before_launch"])


engine.save = save
try:
    result = engine.run(create="--create" in sys.argv, resume_unknown="--resume-unknown" in sys.argv)
    print(json.dumps(result))
except a.Stop as error:
    print(json.dumps({"state": "STOPPED", "reason": str(error)}))
    raise SystemExit(2)
